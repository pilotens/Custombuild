"""Independent verification and backplotting for executable CAM candidates.

The toolpath generator deliberately lives in :mod:`custombuild_cam.toolpaths`.
This module does not import it or reuse any of its private geometry helpers.  It
reconstructs safety envelopes and operation coverage from the public source
operation and production-toolpath contracts so a defect in the generator is
not automatically repeated by the verifier.

Passing this verifier means that the candidate is internally consistent and
bounded by the supplied machine/setup contracts.  It never authorizes a
physical machine start; workshop acceptance remains mandatory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from html import escape
from math import isqrt
from typing import Any, TypeVar, cast

from custombuild_manufacturing.model import (
    CAMOperation,
    OperationKind,
    OperationsDocument,
    Rect,
    Setup,
    Side,
    ToolSpec,
    canonical_data,
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
    EXECUTABLE_CAM_CANDIDATE_MODE,
    FIXTURE_KEEPOUT_POLICY,
    IDENTITY_SOURCE_TO_WCS_XY,
    MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
    MAX_THROUGH_OVERTRAVEL_UM,
    STOCK_TOP_Z0_REFERENCE,
    BoundSetup,
    CuttingRecipe,
    ProductionExecutionContext,
    ProductionMove,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolGeometry,
    ProductionToolpathDocument,
)
from .validation import validate_operations_document

CUTTING_PROGRAM_REPORT_SCHEMA_VERSION = "custombuild.cutting-program-report.v1"
CUTTING_PROGRAM_VERIFIER_VERSION = "cutting-program-verifier-1.1.0"
CUTTING_BACKPLOT_VERSION = "cutting-backplot-1.1.0"
_ARC_RADIAL_TOLERANCE_UM = 4
_MAX_CORNER_CHORD_PPM = 196_000
# Canonical public CAM v1 rounded corners use 11.25-degree integer chords.  The
# verifier owns its own fixed-point reconstruction instead of importing the
# generator's table or interpolation helper.
_CANONICAL_QUARTER_ARC_PPM = (
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
_SVG_SETUP_GAP_UM = 30_000
_IDENTITY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_UNRESOLVED_EVIDENCE_TOKENS = (
    "DEMO",
    "EXAMPLE",
    "EXTERNAL",
    "FAKE",
    "GENERIC",
    "MOCK",
    "PLACEHOLDER",
    "REFERENCE",
    "REQUIRED",
    "SCREENING",
    "TBD",
    "TEST",
    "TODO",
    "UNKNOWN",
    "UNRESOLVED",
    "UNVERIFIED",
    "VALIDATION",
)
_SUPPORTED_OPERATION_KINDS = frozenset(
    {
        OperationKind.DRILL,
        OperationKind.POCKET,
        OperationKind.GROOVE,
        OperationKind.CONTOUR,
    }
)
_T = TypeVar("_T")


class CuttingProgramStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 -- verification status, not a credential
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class CuttingProgramIssue:
    code: str
    message: str
    program_id: str | None = None
    operation_id: str | None = None
    move_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class SweptEnvelope:
    """Conservative cutter envelope for one G0/G1-equivalent move."""

    program_id: str
    setup_id: str
    tool_id: str
    operation_id: str
    move_sequence: int
    motion_kind: str
    cutter_radius_um: int
    process_accuracy_um: int
    assembly_collision_radius_um: int
    x_min_um: int
    y_min_um: int
    z_min_um: int
    x_max_um: int
    y_max_um: int
    z_max_um: int
    machine_x_min_um: int
    machine_y_min_um: int
    machine_z_min_um: int
    machine_x_max_um: int
    machine_y_max_um: int
    machine_z_max_um: int
    assembly_x_min_um: int
    assembly_y_min_um: int
    assembly_x_max_um: int
    assembly_y_max_um: int
    material_removal: bool
    material_z_min_um: int | None
    material_z_max_um: int | None


@dataclass(frozen=True, slots=True)
class CuttingProgramReport:
    schema_version: str
    verifier_version: str
    status: CuttingProgramStatus
    toolpath_sha256: str
    operations_sha256: str
    swept_envelopes_sha256: str
    program_count: int
    operation_count: int
    move_count: int
    material_removal_move_count: int
    issue_count: int
    issues: tuple[CuttingProgramIssue, ...]
    physical_cutting_authorized: bool = False
    workshop_acceptance_required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], canonical_data(self))

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)


@dataclass(frozen=True, slots=True)
class CuttingProgramVerification:
    report: CuttingProgramReport
    swept_envelopes: tuple[SweptEnvelope, ...]


@dataclass(frozen=True, slots=True)
class _Point3D:
    x_um: int
    y_um: int
    z_um: int


@dataclass(frozen=True, slots=True)
class _PartOutline:
    stock_id: str
    sheet_index: int
    instance_id: str
    part_id: str
    source_rotation_90: bool
    operation_id: str
    rect: Rect


class _Issues:
    def __init__(self) -> None:
        self._values: set[CuttingProgramIssue] = set()

    def add(
        self,
        code: str,
        message: str,
        *,
        program: ProductionProgram | None = None,
        operation_id: str | None = None,
        move: ProductionMove | None = None,
    ) -> None:
        self._values.add(
            CuttingProgramIssue(
                code=code,
                message=message,
                program_id=program.program_id if program else None,
                operation_id=operation_id or (move.operation_id if move else None),
                move_sequence=move.sequence if move else None,
            )
        )

    def sorted(self) -> tuple[CuttingProgramIssue, ...]:
        return tuple(
            sorted(
                self._values,
                key=lambda issue: (
                    issue.code,
                    issue.program_id or "",
                    issue.operation_id or "",
                    issue.move_sequence or 0,
                    issue.message,
                ),
            )
        )


def verify_production_toolpaths(
    document: ProductionToolpathDocument,
    source: OperationsDocument,
) -> CuttingProgramVerification:
    """Independently validate a production candidate against source operations."""

    issues = _Issues()
    context = document.execution_context
    expected_operations_sha256 = sha256_hex(source.to_json())

    source_validation = validate_operations_document(source)
    for error in source_validation.errors:
        issues.add("SOURCE_CONTRACT_INVALID", error)
    _validate_source_machine_binding(source, context, issues)
    _validate_execution_catalogs(context, issues)
    if source.design_hash != document.design_hash:
        issues.add("DOCUMENT_BINDING_MISMATCH", "source and toolpath design hashes differ")
    if document.operations_sha256 != expected_operations_sha256:
        issues.add("DOCUMENT_BINDING_MISMATCH", "source operations checksum differs")
    if document.mode != EXECUTABLE_CAM_CANDIDATE_MODE:
        issues.add("CANDIDATE_MODE_INVALID", "toolpath mode is not executable CAM candidate")
    if document.physical_cutting_authorized is not False:
        issues.add("PHYSICAL_AUTHORITY_INVALID", "CAM candidate claims physical authority")
    if document.workshop_acceptance_required is not True:
        issues.add("WORKSHOP_ACCEPTANCE_BYPASSED", "workshop acceptance is not required")
    if (
        source.machine_profile_id != context.source_machine_profile_id
        or source.machine_profile_version != context.source_machine_profile_version
    ):
        issues.add(
            "DOCUMENT_BINDING_MISMATCH",
            "source validation-machine profile identity differs",
        )
    if document.machine_profile_fingerprint != context.machine_profile_fingerprint:
        issues.add("DOCUMENT_BINDING_MISMATCH", "machine profile fingerprint differs")
    if document.tool_catalog_fingerprint != context.tool_catalog_fingerprint:
        issues.add("DOCUMENT_BINDING_MISMATCH", "tool catalogue fingerprint differs")
    if document.recipe_catalog_fingerprint != context.recipe_catalog_fingerprint:
        issues.add("DOCUMENT_BINDING_MISMATCH", "recipe catalogue fingerprint differs")

    source_setups = _unique_by_id(
        source.setups,
        lambda item: item.setup_id,
        issues,
        "SOURCE_SETUP_DUPLICATE",
    )
    bound_setups = _unique_by_id(
        context.setups,
        lambda item: item.setup_id,
        issues,
        "BOUND_SETUP_DUPLICATE",
    )
    bindings = _unique_by_id(
        context.tool_bindings,
        lambda item: item.tool_id,
        issues,
        "TOOL_BINDING_DUPLICATE",
    )
    bindings_by_source_tool = _unique_by_id(
        context.tool_bindings,
        lambda item: item.source_tool_id,
        issues,
        "SOURCE_TOOL_BINDING_DUPLICATE",
    )
    source_tools = _unique_by_id(
        source.tools,
        lambda item: item.tool_id,
        issues,
        "SOURCE_TOOL_DUPLICATE",
    )
    source_operations = _unique_by_id(
        source.operations,
        lambda item: item.operation_id,
        issues,
        "SOURCE_OPERATION_DUPLICATE",
    )
    recipes = _unique_by_id(
        context.recipes,
        lambda item: item.recipe_id,
        issues,
        "RECIPE_DUPLICATE",
    )

    _validate_setup_bindings(
        source_setups,
        bound_setups,
        bindings_by_source_tool,
        context,
        source.operations,
        issues,
    )
    _validate_source_tool_bindings(
        source_tools,
        bindings_by_source_tool,
        source_operations,
        issues,
    )
    part_outlines = _reconstruct_part_outlines(source.operations, source_setups, issues)
    _validate_program_order(document.programs, issues)
    _validate_program_partition(
        document.programs,
        source.operations,
        source_setups,
        bound_setups,
        bindings_by_source_tool,
        tuple(recipes.values()),
        context,
        issues,
    )

    for operation in source.operations:
        if operation.kind not in _SUPPORTED_OPERATION_KINDS:
            issues.add(
                "SOURCE_OPERATION_UNSUPPORTED",
                "source operation kind is unsupported by production CAM v1",
                operation_id=operation.operation_id,
            )

    envelopes: list[SweptEnvelope] = []
    covered_operations: list[str] = []
    released_sheets: set[tuple[str, int]] = set()
    for expected_run_order, program in enumerate(document.programs, start=1):
        setup = bound_setups.get(program.setup_id)
        binding = bindings.get(program.tool_id)
        if program.run_order != expected_run_order:
            issues.add(
                "PROGRAM_ORDER_INVALID",
                "program run order must be dense and canonical",
                program=program,
            )
        if setup is None:
            issues.add(
                "PROGRAM_SETUP_UNKNOWN",
                "program references an unknown bound setup",
                program=program,
            )
            continue
        if binding is None or binding.tool_version != program.tool_version:
            issues.add(
                "PROGRAM_TOOL_UNKNOWN",
                "program tool identity/version is not bound",
                program=program,
            )
            continue

        program_operations = tuple(
            source_operations[operation_id]
            for operation_id in program.operation_ids
            if operation_id in source_operations
        )
        if len(program_operations) != len(program.operation_ids):
            issues.add(
                "PROGRAM_OPERATION_UNKNOWN",
                "program contains an operation absent from the source document",
                program=program,
            )
        for operation in program_operations:
            covered_operations.append(operation.operation_id)
            if (
                operation.setup_id != program.setup_id
                or operation.side != setup.side
                or operation.tool_id != binding.source_tool_id
            ):
                issues.add(
                    "PROGRAM_OPERATION_BINDING_INVALID",
                    "operation does not match its program setup/source-tool binding",
                    program=program,
                    operation_id=operation.operation_id,
                )
            source_tool = source_tools.get(operation.tool_id)
            if source_tool is not None:
                _validate_operation_contract(
                    program,
                    operation,
                    setup,
                    source_tool,
                    binding,
                    issues,
                )

        operation_recipes = _recipes_for_operations(
            program,
            program_operations,
            setup,
            binding,
            tuple(recipes.values()),
            context.machine_profile_id,
            context.machine_profile_version,
            issues,
        )
        expected_recipe_ids = tuple(
            sorted({recipe.recipe_id for recipe in operation_recipes.values()})
        )
        if program.recipe_ids != expected_recipe_ids:
            issues.add(
                "PROGRAM_RECIPE_BINDING_INVALID",
                "program recipe inventory differs from its operations",
                program=program,
            )

        expected_release_ids = tuple(
            operation.operation_id
            for operation in program_operations
            if _is_release_contour(operation)
        )
        if program.release_operation_ids != expected_release_ids:
            issues.add(
                "RELEASE_ORDER_INVALID",
                "release-operation inventory differs from source contour semantics",
                program=program,
            )
        if expected_release_ids and program.operation_ids[-len(expected_release_ids) :] != (
            expected_release_ids
        ):
            issues.add(
                "RELEASE_ORDER_INVALID",
                "release contours are not the final program operations",
                program=program,
            )
        if expected_release_ids and program.operation_ids != expected_release_ids:
            issues.add(
                "RELEASE_ORDER_INVALID",
                "terminal release operations must be isolated in their own program",
                program=program,
            )
        physical_sheet = (setup.stock_id, setup.sheet_index)
        if physical_sheet in released_sheets:
            issues.add(
                "RELEASE_ORDER_INVALID",
                "a program follows a terminal release contour on the physical sheet",
                program=program,
            )
        if expected_release_ids:
            released_sheets.add(physical_sheet)

        _validate_program_entry(program, setup, binding, context, issues)
        _validate_move_order(program, issues)
        program_envelopes = _build_program_envelopes(
            program,
            setup,
            binding,
            operation_recipes,
        )
        envelopes.extend(program_envelopes)
        _validate_motion_safety(program, setup, binding, program_envelopes, context, issues)
        _validate_inter_part_collisions(
            program,
            setup,
            program_envelopes,
            source_operations,
            part_outlines,
            issues,
        )
        for operation in program_operations:
            recipe = operation_recipes.get(operation.operation_id)
            if recipe is not None and operation.kind in _SUPPORTED_OPERATION_KINDS:
                _validate_recipe_operation_contract(
                    program,
                    operation,
                    setup,
                    binding,
                    recipe,
                    issues,
                )
                _validate_operation_motion(program, operation, setup, binding, recipe, issues)

    expected_operation_ids = tuple(source_operations)
    if len(covered_operations) != len(set(covered_operations)) or set(covered_operations) != set(
        expected_operation_ids
    ):
        issues.add(
            "PROGRAM_OPERATION_COVERAGE_INVALID",
            "programs do not cover every source operation exactly once",
        )

    ordered_envelopes = tuple(
        sorted(envelopes, key=lambda item: (item.program_id, item.move_sequence))
    )
    issue_values = issues.sorted()
    report = CuttingProgramReport(
        schema_version=CUTTING_PROGRAM_REPORT_SCHEMA_VERSION,
        verifier_version=CUTTING_PROGRAM_VERIFIER_VERSION,
        status=CuttingProgramStatus.BLOCK if issue_values else CuttingProgramStatus.PASS,
        toolpath_sha256=document.fingerprint,
        operations_sha256=expected_operations_sha256,
        swept_envelopes_sha256=sha256_hex(canonical_json_bytes(ordered_envelopes)),
        program_count=len(document.programs),
        operation_count=len(source.operations),
        move_count=sum(len(program.moves) for program in document.programs),
        material_removal_move_count=sum(item.material_removal for item in ordered_envelopes),
        issue_count=len(issue_values),
        issues=issue_values,
        physical_cutting_authorized=False,
        workshop_acceptance_required=True,
    )
    return CuttingProgramVerification(report=report, swept_envelopes=ordered_envelopes)


def cutting_program_report_json(
    document: ProductionToolpathDocument,
    source: OperationsDocument,
) -> bytes:
    return verify_production_toolpaths(document, source).report.to_json()


def cutting_backplot_svg(
    document: ProductionToolpathDocument,
    source: OperationsDocument,
) -> bytes:
    """Render a deterministic inert SVG from independently verified public moves."""

    verification = verify_production_toolpaths(document, source)
    report = verification.report
    setup_by_id = {setup.setup_id: setup for setup in document.execution_context.setups}
    binding_by_id = {
        binding.tool_id: binding for binding in document.execution_context.tool_bindings
    }
    setup_order = tuple(setup_by_id)
    y_offsets: dict[str, int] = {}
    current_y = 0
    canvas_width = 100_000
    for setup_id in setup_order:
        setup = setup_by_id[setup_id]
        y_offsets[setup_id] = current_y
        canvas_width = max(canvas_width, setup.stock_width_um)
        current_y += setup.stock_height_um + _SVG_SETUP_GAP_UM
    canvas_height = max(100_000, current_y - _SVG_SETUP_GAP_UM)

    fragments = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {canvas_width} {canvas_height}" '
            f'data-backplot-version="{CUTTING_BACKPLOT_VERSION}" '
            f'data-verification-status="{report.status.value}" '
            f'data-toolpath-sha256="{report.toolpath_sha256}" '
            f'data-operations-sha256="{report.operations_sha256}" '
            f'data-swept-envelopes-sha256="{report.swept_envelopes_sha256}" '
            f'data-report-sha256="{sha256_hex(report.to_json())}" '
            'data-mode="EXECUTABLE_CAM_CANDIDATE" '
            'data-physical-cutting-authorized="false" '
            'data-workshop-acceptance-required="true">'
        ),
        "<title>Custombuild executable CAM candidate cutting backplot</title>",
        (
            "<style>"
            ".stock{fill:#f8fafc;stroke:#0f172a;stroke-width:1000}"
            ".keepout{fill:#ef444433;stroke:#b91c1c;stroke-width:900}"
            ".rapid{fill:none;stroke:#64748b;stroke-width:600;stroke-dasharray:4000 2500}"
            ".cut{fill:none;stroke-width:1000}"
            ".plunge{fill-opacity:.12}"
            ".blocked{stroke:#dc2626!important}"
            ".label{font:10000px sans-serif;fill:#0f172a}"
            "</style>"
        ),
    ]
    for setup_id in setup_order:
        setup = setup_by_id[setup_id]
        y_offset = y_offsets[setup_id]
        fragments.append(
            f'<g data-setup-id="{_escape_svg_metadata(setup_id)}" '
            f'data-machine-wcs-x0-um="{setup.machine_wcs_origin.x_um}" '
            f'data-machine-wcs-y0-um="{setup.machine_wcs_origin.y_um}" '
            f'data-machine-wcs-z0-um="{setup.machine_wcs_z0_um}" '
            f'data-machine-wcs-xy-rotation-mdeg="{setup.machine_wcs_xy_rotation_mdeg}" '
            f'transform="translate(0 {y_offset})">'
        )
        fragments.append(
            f'<rect class="stock" x="0" y="0" width="{setup.stock_width_um}" '
            f'height="{setup.stock_height_um}"/>'
        )
        for zone_index, zone in enumerate(setup.keep_out_zones, start=1):
            fragments.append(
                f'<rect class="keepout" data-zone-index="{zone_index}" '
                f'x="{zone.x_um}" y="{zone.y_um}" width="{zone.width_um}" '
                f'height="{zone.height_um}"/>'
            )
        fragments.append(
            f'<text class="label" x="5000" y="15000">{_escape_svg_metadata(setup_id)}</text>'
        )
        for program in document.programs:
            if program.setup_id != setup_id:
                continue
            binding = binding_by_id.get(program.tool_id)
            fragments.append(
                f'<g data-program-id="{_escape_svg_metadata(program.program_id)}" '
                f'data-run-order="{program.run_order}" '
                f'data-tool-id="{_escape_svg_metadata(program.tool_id)}"'
                + (
                    f' data-h-offset-x-um="{binding.expected_length_offset_x_um}" '
                    f'data-h-offset-y-um="{binding.expected_length_offset_y_um}" '
                    f'data-h-offset-z-um="{binding.expected_length_offset_z_um}" '
                    'data-coordinate-frame="PROGRAMMED_WCS_TOOL_TIP_AND_'
                    'G43_MACHINE_CONTROLLED_POINT" '
                    f'data-tool-table-evidence-sha256="{binding.tool_table_evidence_sha256}"'
                    if binding is not None
                    else ""
                )
                + ">"
            )
            if not program.moves:
                fragments.append("</g>")
                continue
            previous = _Point3D(
                program.moves[0].x_um,
                program.moves[0].y_um,
                setup.safe_z_um,
            )
            for move in program.moves:
                css_class = "rapid" if move.kind == ProductionMoveKind.RAPID else "cut"
                if report.status == CuttingProgramStatus.BLOCK:
                    css_class += " blocked"
                stroke = (
                    "#64748b"
                    if move.kind == ProductionMoveKind.RAPID
                    else _cutting_stroke(program.run_order, move.pass_index)
                )
                metadata = (
                    f'data-sequence="{move.sequence}" '
                    f'data-operation-id="{_escape_svg_metadata(move.operation_id)}" '
                    f'data-motion-kind="{move.kind.value}" '
                    f'data-role="{move.role.value}" '
                    f'data-pass-index="{move.pass_index}" '
                    f'data-start-depth-um="{previous.z_um}" '
                    f'data-depth-um="{move.z_um}"'
                )
                if binding is not None:
                    machine_x_um = (
                        setup.machine_wcs_origin.x_um
                        + move.x_um
                        + binding.expected_length_offset_x_um
                    )
                    machine_y_um = (
                        setup.machine_wcs_origin.y_um
                        + move.y_um
                        + binding.expected_length_offset_y_um
                    )
                    machine_z_um = (
                        setup.machine_wcs_z0_um + move.z_um + binding.expected_length_offset_z_um
                    )
                    machine_start_x_um = (
                        setup.machine_wcs_origin.x_um
                        + previous.x_um
                        + binding.expected_length_offset_x_um
                    )
                    machine_start_y_um = (
                        setup.machine_wcs_origin.y_um
                        + previous.y_um
                        + binding.expected_length_offset_y_um
                    )
                    machine_start_z_um = (
                        setup.machine_wcs_z0_um
                        + previous.z_um
                        + binding.expected_length_offset_z_um
                    )
                    metadata += (
                        f' data-machine-start-x-um="{machine_start_x_um}" '
                        f'data-machine-start-y-um="{machine_start_y_um}" '
                        f'data-machine-start-z-um="{machine_start_z_um}" '
                        f'data-machine-x-um="{machine_x_um}" '
                        f'data-machine-y-um="{machine_y_um}" '
                        f'data-machine-z-um="{machine_z_um}"'
                    )
                if (
                    move.kind == ProductionMoveKind.LINEAR
                    and previous.x_um == move.x_um
                    and previous.y_um == move.y_um
                    and binding is not None
                ):
                    fragments.append(
                        f'<circle class="{css_class} plunge" {metadata} '
                        f'stroke="{stroke}" cx="{move.x_um}" cy="{move.y_um}" '
                        f'r="{(binding.effective_diameter_um + 1) // 2}"/>'
                    )
                else:
                    fragments.append(
                        f'<line class="{css_class}" {metadata} stroke="{stroke}" '
                        f'x1="{previous.x_um}" y1="{previous.y_um}" '
                        f'x2="{move.x_um}" y2="{move.y_um}"/>'
                    )
                previous = _Point3D(move.x_um, move.y_um, move.z_um)
            fragments.append("</g>")
        fragments.append("</g>")
    fragments.append("</svg>")
    return "".join(fragments).encode("utf-8")


def _cutting_stroke(run_order: int, pass_index: int) -> str:
    palette = (
        "#0369a1",
        "#047857",
        "#7c3aed",
        "#b45309",
        "#be123c",
        "#0f766e",
    )
    return palette[(run_order + pass_index - 2) % len(palette)]


def _escape_svg_metadata(value: object) -> str:
    """Escape text and replace code points forbidden by XML 1.0."""

    raw = str(value)
    xml_safe = "".join(
        character
        if (
            character in "\t\n\r"
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
        else "\N{REPLACEMENT CHARACTER}"
        for character in raw
    )
    return escape(xml_safe, quote=True)


def _unique_by_id(
    values: tuple[_T, ...],
    identity: Callable[[_T], str],
    issues: _Issues,
    issue_code: str,
) -> dict[str, _T]:
    result: dict[str, _T] = {}
    for value in values:
        value_id = identity(value)
        if value_id in result:
            issues.add(issue_code, f"duplicate identity: {value_id}")
        else:
            result[value_id] = value
    return result


def _validate_source_machine_binding(
    source: OperationsDocument,
    context: ProductionExecutionContext,
    issues: _Issues,
) -> None:
    factories = {
        REFERENCE_MACHINE_PROFILE_ID: linuxcnc_reference_router_1325,
        LARGE_FORMAT_MACHINE_PROFILE_ID: linuxcnc_reference_router_5125,
    }
    factory = factories.get(source.machine_profile_id)
    if factory is None:
        issues.add(
            "SOURCE_MACHINE_BINDING_INVALID",
            "source validation-machine profile is not trusted",
        )
        return
    trusted_machine = factory()
    if (
        source.machine_profile_version != trusted_machine.version
        or context.source_machine_profile_id != trusted_machine.profile_id
        or context.source_machine_profile_version != trusted_machine.version
        or context.source_machine_profile_fingerprint
        != sha256_hex(canonical_json_bytes(trusted_machine))
    ):
        issues.add(
            "SOURCE_MACHINE_BINDING_INVALID",
            "source validation-machine profile identity or fingerprint differs",
        )


def _validate_execution_catalogs(
    context: ProductionExecutionContext,
    issues: _Issues,
) -> None:
    controller_numbers = tuple(binding.controller_tool_number for binding in context.tool_bindings)
    length_offsets = tuple(binding.length_offset_number for binding in context.tool_bindings)
    if len(controller_numbers) != len(set(controller_numbers)):
        issues.add(
            "TOOL_BINDING_DUPLICATE",
            "controller tool numbers are not unique",
        )
    if len(length_offsets) != len(set(length_offsets)):
        issues.add(
            "TOOL_BINDING_DUPLICATE",
            "controller length-offset numbers are not unique",
        )
    tool_table_evidence = tuple(
        (
            binding.tool_table_evidence_id,
            binding.tool_table_evidence_version,
            binding.tool_table_evidence_sha256,
        )
        for binding in context.tool_bindings
    )
    if not tool_table_evidence or any(
        evidence != tool_table_evidence[0] for evidence in tool_table_evidence[1:]
    ):
        issues.add(
            "TOOL_TABLE_BINDING_INVALID",
            "tool bindings do not share one atomic controller tool-table snapshot",
        )
    for binding in context.tool_bindings:
        if (
            type(binding.controller_tool_number) is not int
            or binding.controller_tool_number <= 0
            or binding.controller_tool_number > MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
            or type(binding.length_offset_number) is not int
            or binding.length_offset_number <= 0
            or binding.length_offset_number > MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
            or type(binding.expected_length_offset_x_um) is not int
            or type(binding.expected_length_offset_y_um) is not int
            or type(binding.expected_length_offset_z_um) is not int
            or binding.expected_length_offset_x_um != 0
            or binding.expected_length_offset_y_um != 0
            or not isinstance(binding.tool_table_evidence_id, str)
            or not _is_resolved_evidence_identity(binding.tool_table_evidence_id)
            or not isinstance(binding.tool_table_evidence_version, str)
            or not _is_resolved_evidence_identity(binding.tool_table_evidence_version)
            or not isinstance(binding.tool_table_evidence_sha256, str)
            or not _is_sha256(binding.tool_table_evidence_sha256)
        ):
            issues.add(
                "TOOL_TABLE_BINDING_INVALID",
                f"tool-table offset/evidence binding is invalid: {binding.tool_id}",
            )
        if (
            binding.effective_diameter_um <= 0
            or binding.effective_diameter_um % 2
            or binding.cutting_length_um <= 0
            or binding.measured_stickout_um <= 0
            or binding.minimum_holder_clearance_um <= 0
            or binding.cutting_length_um > binding.measured_stickout_um
            or binding.minimum_holder_clearance_um >= binding.measured_stickout_um
            or 2 * binding.assembly_collision_radius_um < binding.effective_diameter_um
        ):
            issues.add(
                "TOOL_BINDING_INVALID",
                f"production tool dimensions/clearances are invalid: {binding.tool_id}",
            )
        if (
            type(binding.drill_point_length_um) is not int
            or binding.drill_point_length_um != 0
            or (binding.geometry == ProductionToolGeometry.DRILL and not binding.center_cutting)
        ):
            issues.add(
                "DRILL_POINT_GEOMETRY_INVALID",
                "production CAM v1 requires zero drill point length and a "
                f"center-cutting production drill: {binding.tool_id}",
            )
    recipe_keys: set[tuple[str, str, str, str, OperationKind]] = set()
    for recipe in context.recipes:
        key = (
            recipe.material_id,
            recipe.material_version,
            recipe.tool_id,
            recipe.tool_version,
            recipe.operation_kind,
        )
        if key in recipe_keys:
            issues.add(
                "RECIPE_DUPLICATE",
                "multiple recipes claim one material/tool/operation binding",
            )
        recipe_keys.add(key)
        if (
            recipe.machine_profile_id != context.machine_profile_id
            or recipe.machine_profile_version != context.machine_profile_version
            or not context.min_spindle_rpm <= recipe.spindle_rpm <= context.max_spindle_rpm
            or recipe.feed_um_min > context.max_feed_um_min
            or recipe.plunge_um_min > context.max_plunge_um_min
            or recipe.process_accuracy_um <= 0
            or recipe.process_accuracy_um > recipe.accepted_tolerance_um
        ):
            issues.add(
                "RECIPE_MACHINE_LIMIT_INVALID",
                f"recipe identity or parameters exceed the machine contract: {recipe.recipe_id}",
            )


def _validate_setup_bindings(
    source_setups: dict[str, Setup],
    bound_setups: dict[str, BoundSetup],
    bindings_by_source_tool: dict[str, ProductionToolBinding],
    context: ProductionExecutionContext,
    operations: tuple[CAMOperation, ...],
    issues: _Issues,
) -> None:
    absolute_bounds = (
        (
            "X",
            context.machine_x_min_um,
            context.machine_x_max_um,
            context.work_width_um,
        ),
        (
            "Y",
            context.machine_y_min_um,
            context.machine_y_max_um,
            context.work_height_um,
        ),
        (
            "Z",
            context.machine_z_min_um,
            context.machine_z_max_um,
            context.work_z_um,
        ),
    )
    for axis, minimum, maximum, declared_travel in absolute_bounds:
        if minimum >= maximum or maximum - minimum != declared_travel:
            issues.add(
                "MACHINE_ABSOLUTE_BOUNDS_INVALID",
                f"machine {axis} bounds do not exactly bind the declared travel",
            )
    if set(source_setups) != set(bound_setups):
        issues.add("SETUP_BINDING_INVALID", "bound setups do not exactly cover source setups")
    physical_sheet_materials: dict[tuple[str, int], tuple[str, str, str, str, str, str, str]] = {}
    for setup_id, source in source_setups.items():
        bound = bound_setups.get(setup_id)
        if bound is None:
            continue
        source_geometry = (
            source.stock_id,
            source.material_id,
            source.material_version,
            source.sheet_index,
            source.side,
            source.stock_width_um,
            source.stock_height_um,
            source.stock_thickness_um,
        )
        bound_geometry = (
            bound.stock_id,
            bound.source_material_id,
            bound.source_material_version,
            bound.sheet_index,
            bound.side,
            bound.stock_width_um,
            bound.stock_height_um,
            bound.stock_thickness_um,
        )
        if source_geometry != bound_geometry:
            issues.add(
                "SETUP_BINDING_INVALID",
                f"bound setup geometry differs from source: {setup_id}",
            )
        if (
            not _is_resolved_evidence_identity(bound.material_id)
            or not _is_resolved_evidence_identity(bound.material_version)
            or not _is_resolved_evidence_identity(bound.material_evidence_id)
            or not _is_resolved_evidence_identity(bound.material_evidence_version)
            or not _is_sha256(bound.material_evidence_sha256)
        ):
            issues.add(
                "MATERIAL_BINDING_INVALID",
                f"actual material identity or evidence is invalid: {setup_id}",
            )
        physical_sheet = (bound.stock_id, bound.sheet_index)
        material_binding = (
            bound.source_material_id,
            bound.source_material_version,
            bound.material_id,
            bound.material_version,
            bound.material_evidence_id,
            bound.material_evidence_version,
            bound.material_evidence_sha256,
        )
        previous_material = physical_sheet_materials.setdefault(physical_sheet, material_binding)
        if previous_material != material_binding:
            issues.add(
                "MATERIAL_BINDING_INVALID",
                "setups for one physical sheet disagree on source or actual material: "
                f"{bound.stock_id}:{bound.sheet_index}",
            )
        if bound.source_setup_sha256 != sha256_hex(canonical_json_bytes(source)):
            issues.add(
                "SETUP_BINDING_INVALID",
                f"bound setup source fingerprint differs: {setup_id}",
            )
        if bound.orientation != source.orientation:
            issues.add(
                "SETUP_BINDING_INVALID",
                f"bound setup orientation differs from source: {setup_id}",
            )
        if (
            bound.source_to_wcs_xy_transform != IDENTITY_SOURCE_TO_WCS_XY
            or bound.reference_surface != STOCK_TOP_Z0_REFERENCE
            or bound.keep_out_policy != FIXTURE_KEEPOUT_POLICY
            or bound.wcs not in {"G54", "G55", "G56", "G57", "G58", "G59"}
            or bound.raw_allowance_um != 0
        ):
            issues.add(
                "SETUP_BINDING_INVALID",
                f"bound setup transform/WCS/reference policy is not exact: {setup_id}",
            )
        if (
            type(bound.machine_wcs_xy_rotation_mdeg) is not int
            or bound.machine_wcs_xy_rotation_mdeg != 0
        ):
            issues.add(
                "WCS_ROTATION_INVALID",
                f"production WCS has a nonzero or unbound XY rotation: {setup_id}",
            )
        if (
            not _is_canonical_identity(bound.fixture_id)
            or not _is_canonical_identity(bound.fixture_version)
            or not _is_sha256(bound.fixture_sha256)
        ):
            issues.add(
                "FIXTURE_BINDING_INVALID",
                f"setup fixture identity or checksum is invalid: {setup_id}",
            )
        if not set(source.keep_out_zones) <= set(bound.keep_out_zones):
            issues.add("SETUP_BINDING_INVALID", f"bound setup omits keep-out zones: {setup_id}")
        if bound.reference_surface != STOCK_TOP_Z0_REFERENCE:
            issues.add(
                "SETUP_Z_REFERENCE_INVALID",
                f"bound setup does not define stock top as Z0: {setup_id}",
            )
        if bound.fixture_clearance_z_um + bound.minimum_rapid_clearance_um > bound.safe_z_um:
            issues.add(
                "SETUP_Z_LIMIT",
                f"setup safe Z lacks its exact fixture/rapid-clearance margin: {setup_id}",
            )
        setup_has_through_cut = any(
            operation.through and operation.setup_id == setup_id for operation in operations
        )
        spoilboard_values = (
            bound.spoilboard_id,
            bound.spoilboard_version,
            bound.spoilboard_sha256,
        )
        valid_spoilboard_identity = (
            isinstance(bound.spoilboard_id, str)
            and _is_canonical_identity(bound.spoilboard_id)
            and isinstance(bound.spoilboard_version, str)
            and _is_canonical_identity(bound.spoilboard_version)
            and isinstance(bound.spoilboard_sha256, str)
            and _is_sha256(bound.spoilboard_sha256)
        )
        if setup_has_through_cut and (
            not 1 <= bound.through_cut_allowance_um <= MAX_THROUGH_OVERTRAVEL_UM
            or not valid_spoilboard_identity
        ):
            issues.add(
                "SPOILBOARD_BINDING_INVALID",
                f"through-cut setup lacks an exact allowance/spoilboard binding: {setup_id}",
            )
        if not setup_has_through_cut and (
            bound.through_cut_allowance_um != 0
            or any(value is not None for value in spoilboard_values)
        ):
            issues.add(
                "SPOILBOARD_BINDING_INVALID",
                f"non-through setup declares a spoilboard allowance/binding: {setup_id}",
            )
        setup_bindings = {
            operation.tool_id: bindings_by_source_tool[operation.tool_id]
            for operation in operations
            if operation.setup_id == setup_id and operation.tool_id in bindings_by_source_tool
        }
        for source_tool_id, tool_binding in setup_bindings.items():
            machine_x_min_um = (
                bound.machine_wcs_origin.x_um + tool_binding.expected_length_offset_x_um
            )
            machine_x_max_um = machine_x_min_um + bound.stock_width_um
            machine_y_min_um = (
                bound.machine_wcs_origin.y_um + tool_binding.expected_length_offset_y_um
            )
            machine_y_max_um = machine_y_min_um + bound.stock_height_um
            if (
                machine_x_min_um < context.machine_x_min_um
                or machine_x_max_um > context.machine_x_max_um
                or machine_y_min_um < context.machine_y_min_um
                or machine_y_max_um > context.machine_y_max_um
            ):
                issues.add(
                    "SETUP_XY_LIMIT",
                    "G43-transformed setup stock envelope exceeds absolute machine "
                    f"travel for {setup_id}/{source_tool_id}",
                )
            machine_safe_z_um = (
                bound.machine_wcs_z0_um + bound.safe_z_um + tool_binding.expected_length_offset_z_um
            )
            machine_bottom_z_um = (
                bound.machine_wcs_z0_um
                - bound.stock_thickness_um
                - bound.through_cut_allowance_um
                + tool_binding.expected_length_offset_z_um
            )
            if (
                machine_safe_z_um > context.machine_z_max_um
                or machine_bottom_z_um < context.machine_z_min_um
            ):
                issues.add(
                    "SETUP_Z_LIMIT",
                    "G43-transformed setup stock/safe envelope exceeds absolute machine "
                    f"Z travel for {setup_id}/{source_tool_id}",
                )


def _validate_source_tool_bindings(
    source_tools: dict[str, ToolSpec],
    bindings: dict[str, ProductionToolBinding],
    operations: dict[str, CAMOperation],
    issues: _Issues,
) -> None:
    operation_tool_ids = {operation.tool_id for operation in operations.values()}
    if set(source_tools) != operation_tool_ids:
        issues.add(
            "SOURCE_TOOL_COVERAGE_INVALID",
            "source tool snapshot does not exactly cover operation tools",
        )
    if set(bindings) != operation_tool_ids:
        issues.add(
            "SOURCE_TOOL_BINDING_INVALID",
            "production source-tool bindings do not exactly cover operation tools",
        )
    for source_tool_id, source_tool in source_tools.items():
        binding = bindings.get(source_tool_id)
        if binding is None:
            continue
        if (
            binding.source_tool_version != source_tool.version
            or binding.source_tool_sha256 != sha256_hex(canonical_json_bytes(source_tool))
        ):
            issues.add(
                "SOURCE_TOOL_BINDING_INVALID",
                f"production tool provenance differs from source tool: {source_tool_id}",
            )


def _reconstruct_part_outlines(
    operations: tuple[CAMOperation, ...],
    source_setups: dict[str, Setup],
    issues: _Issues,
) -> dict[tuple[str, int, str], _PartOutline]:
    """Recover physical A-frame finished outlines from public source operations.

    The source contract has no separate placement table.  Its one outside-through
    contour per instance is therefore the only independently traceable finished
    placement boundary.  B-side rectangles are reflected back into the physical
    A frame before cross-side comparisons are made.
    """

    instance_binding: dict[str, tuple[str, int, str, bool]] = {}
    releases: dict[str, list[tuple[CAMOperation, Setup, Rect]]] = {}
    for operation in operations:
        setup = source_setups.get(operation.setup_id)
        if setup is None:
            continue
        binding = (
            setup.stock_id,
            setup.sheet_index,
            operation.part_id,
            operation.source_rotation_90,
        )
        previous = instance_binding.setdefault(operation.instance_id, binding)
        if previous != binding:
            issues.add(
                "PART_INSTANCE_BINDING_INVALID",
                "one instance maps to inconsistent part, sheet or nesting rotation facts",
                operation_id=operation.operation_id,
            )
        if not _is_release_contour(operation):
            continue
        if (
            operation.width_um is None
            or operation.length_um is None
            or min(operation.width_um, operation.length_um) <= 0
        ):
            issues.add(
                "PART_OUTLINE_BINDING_INVALID",
                "release contour cannot define a positive finished-part outline",
                operation_id=operation.operation_id,
            )
            continue
        setup_frame_rect = Rect(
            operation.x_um,
            operation.y_um,
            operation.width_um,
            operation.length_um,
        )
        physical_rect = _rect_to_physical_a_frame(setup_frame_rect, setup)
        releases.setdefault(operation.instance_id, []).append((operation, setup, physical_rect))

    outlines: dict[tuple[str, int, str], _PartOutline] = {}
    for instance_id, binding in instance_binding.items():
        candidates = releases.get(instance_id, [])
        if len(candidates) != 1:
            issues.add(
                "PART_OUTLINE_BINDING_INVALID",
                "each instance must have exactly one outside-through finished outline",
                operation_id=(candidates[0][0].operation_id if candidates else None),
            )
            if not candidates:
                continue
        operation, setup, physical_rect = candidates[0]
        stock_id, sheet_index, part_id, source_rotation_90 = binding
        if (
            setup.stock_id != stock_id
            or setup.sheet_index != sheet_index
            or operation.part_id != part_id
            or operation.source_rotation_90 != source_rotation_90
        ):
            issues.add(
                "PART_OUTLINE_BINDING_INVALID",
                "finished outline differs from its instance provenance",
                operation_id=operation.operation_id,
            )
        key = (stock_id, sheet_index, instance_id)
        outlines[key] = _PartOutline(
            stock_id=stock_id,
            sheet_index=sheet_index,
            instance_id=instance_id,
            part_id=part_id,
            source_rotation_90=source_rotation_90,
            operation_id=operation.operation_id,
            rect=physical_rect,
        )

    for operation in operations:
        setup = source_setups.get(operation.setup_id)
        if setup is None:
            continue
        key = (setup.stock_id, setup.sheet_index, operation.instance_id)
        outline = outlines.get(key)
        if outline is None:
            continue
        if (
            operation.part_id != outline.part_id
            or operation.source_rotation_90 != outline.source_rotation_90
        ):
            issues.add(
                "PART_INSTANCE_BINDING_INVALID",
                "operation part/rotation provenance differs from its finished outline",
                operation_id=operation.operation_id,
            )
        active_outline = _rect_from_physical_a_frame(outline.rect, setup)
        nominal = _operation_nominal_rect(operation)
        if nominal is None:
            continue
        if operation.operation_id == outline.operation_id:
            if nominal != active_outline:
                issues.add(
                    "PART_OUTLINE_BINDING_INVALID",
                    "release contour differs from the reconstructed finished outline",
                    operation_id=operation.operation_id,
                )
        elif not active_outline.contains(nominal):
            issues.add(
                "PART_OPERATION_OUTSIDE_OUTLINE",
                "operation nominal geometry leaves its bound finished-part outline",
                operation_id=operation.operation_id,
            )
    ordered_outlines = tuple(
        sorted(
            outlines.values(),
            key=lambda item: (item.stock_id, item.sheet_index, item.instance_id),
        )
    )
    for index, first in enumerate(ordered_outlines):
        for second in ordered_outlines[index + 1 :]:
            if (first.stock_id, first.sheet_index) != (
                second.stock_id,
                second.sheet_index,
            ):
                continue
            if _rectangles_touch_or_overlap(first.rect, second.rect):
                issues.add(
                    "PART_OUTLINE_COLLISION",
                    "finished part-instance outlines touch or overlap on a physical sheet",
                )
    return outlines


def _operation_nominal_rect(operation: CAMOperation) -> Rect | None:
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        if operation.diameter_um is None or operation.diameter_um <= 0:
            return None
        radius_um = operation.diameter_um // 2
        return Rect(
            operation.x_um - radius_um,
            operation.y_um - radius_um,
            operation.diameter_um,
            operation.diameter_um,
        )
    if operation.width_um is None or operation.length_um is None:
        return None
    return Rect(
        operation.x_um,
        operation.y_um,
        operation.width_um,
        operation.length_um,
    )


def _rect_to_physical_a_frame(rect: Rect, setup: Setup | BoundSetup) -> Rect:
    if setup.side != Side.B:
        return rect
    return Rect(
        rect.x_um,
        setup.stock_height_um - rect.top_um,
        rect.width_um,
        rect.height_um,
    )


def _rect_from_physical_a_frame(rect: Rect, setup: Setup | BoundSetup) -> Rect:
    if setup.side != Side.B:
        return rect
    return Rect(
        rect.x_um,
        setup.stock_height_um - rect.top_um,
        rect.width_um,
        rect.height_um,
    )


def _validate_inter_part_collisions(
    program: ProductionProgram,
    setup: BoundSetup,
    envelopes: tuple[SweptEnvelope, ...],
    source_operations: dict[str, CAMOperation],
    outlines: dict[tuple[str, int, str], _PartOutline],
    issues: _Issues,
) -> None:
    sheet_outlines = tuple(
        outline
        for outline in outlines.values()
        if outline.stock_id == setup.stock_id and outline.sheet_index == setup.sheet_index
    )
    start_by_sequence: dict[int, tuple[int, int]] = {}
    previous = (program.moves[0].x_um, program.moves[0].y_um)
    for move in program.moves:
        start_by_sequence[move.sequence] = previous
        previous = (move.x_um, move.y_um)
    move_by_sequence = {move.sequence: move for move in program.moves}
    for envelope in envelopes:
        if not envelope.material_removal:
            continue
        operation = source_operations.get(envelope.operation_id)
        candidate_move = move_by_sequence.get(envelope.move_sequence)
        start = start_by_sequence.get(envelope.move_sequence)
        if operation is None or candidate_move is None or start is None:
            continue
        physical_start = _point_to_physical_a_frame(start, setup)
        physical_end = _point_to_physical_a_frame(
            (candidate_move.x_um, candidate_move.y_um),
            setup,
        )
        effective_radius = envelope.cutter_radius_um + envelope.process_accuracy_um
        for other in sheet_outlines:
            if other.instance_id == operation.instance_id:
                if operation.operation_id == other.operation_id and _is_release_contour(operation):
                    # The one bound release contour intentionally runs outside the
                    # finished outline.  No other operation on the same instance
                    # inherits this exception.
                    continue
                if not _own_part_cutter_sweep_allowed(
                    operation,
                    setup,
                    other,
                    physical_start,
                    physical_end,
                    effective_radius,
                    envelope.process_accuracy_um,
                ):
                    issues.add(
                        "OWN_PART_CUTTER_BREAKOUT",
                        (
                            "accuracy-expanded cutter sweep leaves its own finished-part "
                            "outline without an exactly bound open-end relief"
                        ),
                        program=program,
                        move=candidate_move,
                        operation_id=operation.operation_id,
                    )
                continue
            if _segment_capsule_touches_rect(
                physical_start,
                physical_end,
                effective_radius,
                other.rect,
            ):
                issues.add(
                    "INTER_PART_CUTTER_COLLISION",
                    "accuracy-expanded cutter sweep touches another finished-part outline",
                    program=program,
                    move=candidate_move,
                    operation_id=operation.operation_id,
                )


def _point_to_physical_a_frame(
    point: tuple[int, int],
    setup: BoundSetup,
) -> tuple[int, int]:
    if setup.side != Side.B:
        return point
    return point[0], setup.stock_height_um - point[1]


def _own_part_cutter_sweep_allowed(
    operation: CAMOperation,
    setup: BoundSetup,
    outline: _PartOutline,
    physical_start: tuple[int, int],
    physical_end: tuple[int, int],
    effective_radius_um: int,
    process_accuracy_um: int,
) -> bool:
    """Confine a cut to its own outline with narrow, source-bound edge exits.

    The capsule bounds are exact for containment in an axis-aligned rectangle.
    A groove may cross a finished edge only when the corresponding
    source-local open end maps to that physical edge, the nominal operation
    terminates on the same edge, and the complete accuracy-expanded sweep stays
    inside the independently checked declared cutter envelope.
    """

    x_min = min(physical_start[0], physical_end[0]) - effective_radius_um
    x_max = max(physical_start[0], physical_end[0]) + effective_radius_um
    y_min = min(physical_start[1], physical_end[1]) - effective_radius_um
    y_max = max(physical_start[1], physical_end[1]) + effective_radius_um
    if (
        outline.rect.x_um <= x_min
        and x_max <= outline.rect.right_um
        and outline.rect.y_um <= y_min
        and y_max <= outline.rect.top_um
    ):
        return True

    if (
        operation.kind != OperationKind.GROOVE
        or not operation.open_end_reliefs
        or operation.width_um is None
        or operation.length_um is None
    ):
        return False
    declared_envelope = _declared_cutter_envelope(operation)
    if declared_envelope is None:
        return False
    physical_envelope = _rect_to_physical_a_frame(declared_envelope, setup)
    if not (
        physical_envelope.x_um - process_accuracy_um <= x_min
        and x_max <= physical_envelope.right_um + process_accuracy_um
        and physical_envelope.y_um - process_accuracy_um <= y_min
        and y_max <= physical_envelope.top_um + process_accuracy_um
    ):
        return False

    crossed_boundaries = {
        boundary
        for boundary, crossed in (
            ("x_min", x_min < outline.rect.x_um),
            ("x_max", x_max > outline.rect.right_um),
            ("y_min", y_min < outline.rect.y_um),
            ("y_max", y_max > outline.rect.top_um),
        )
        if crossed
    }
    open_boundaries = _physical_open_end_boundaries(operation)
    if not crossed_boundaries <= open_boundaries:
        return False

    nominal = _rect_to_physical_a_frame(
        Rect(
            operation.x_um,
            operation.y_um,
            operation.width_um,
            operation.length_um,
        ),
        setup,
    )
    boundary_coordinates = {
        "x_min": (nominal.x_um, outline.rect.x_um),
        "x_max": (nominal.right_um, outline.rect.right_um),
        "y_min": (nominal.y_um, outline.rect.y_um),
        "y_max": (nominal.top_um, outline.rect.top_um),
    }
    return all(
        boundary_coordinates[boundary][0] == boundary_coordinates[boundary][1]
        for boundary in crossed_boundaries
    )


def _physical_open_end_boundaries(operation: CAMOperation) -> frozenset[str]:
    if operation.source_rotation_90:
        mapping = {
            "u_min": "y_min",
            "u_max": "y_max",
            "v_min": "x_max",
            "v_max": "x_min",
        }
    else:
        mapping = {
            "u_min": "x_min",
            "u_max": "x_max",
            "v_min": "y_min",
            "v_max": "y_max",
        }
    return frozenset(mapping[value] for value in operation.open_end_reliefs if value in mapping)


def _segment_capsule_touches_rect(
    start: tuple[int, int],
    end: tuple[int, int],
    radius_um: int,
    rectangle: Rect,
) -> bool:
    if _segment_touches_rect(start, end, rectangle):
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
    return any(_point_within_segment_radius(corner, start, end, radius_um) for corner in corners)


def _segment_touches_rect(
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
    edges = tuple(zip(corners, (*corners[1:], corners[0]), strict=True))
    return any(_segments_touch(start, end, edge_start, edge_end) for edge_start, edge_end in edges)


def _point_in_rect(point: tuple[int, int], rectangle: Rect) -> bool:
    return (
        rectangle.x_um <= point[0] <= rectangle.right_um
        and rectangle.y_um <= point[1] <= rectangle.top_um
    )


def _point_rect_distance_squared(point: tuple[int, int], rectangle: Rect) -> int:
    dx = max(rectangle.x_um - point[0], 0, point[0] - rectangle.right_um)
    dy = max(rectangle.y_um - point[1], 0, point[1] - rectangle.top_um)
    return dx * dx + dy * dy


def _point_within_segment_radius(
    point: tuple[int, int],
    start: tuple[int, int],
    end: tuple[int, int],
    radius_um: int,
) -> bool:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2 <= radius_um**2
    projection = (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    if projection <= 0:
        distance_squared = (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2
        return distance_squared <= radius_um**2
    if projection >= length_squared:
        distance_squared = (point[0] - end[0]) ** 2 + (point[1] - end[1]) ** 2
        return distance_squared <= radius_um**2
    cross = dx * (point[1] - start[1]) - dy * (point[0] - start[0])
    return cross * cross <= radius_um**2 * length_squared


def _segments_touch(
    first_start: tuple[int, int],
    first_end: tuple[int, int],
    second_start: tuple[int, int],
    second_end: tuple[int, int],
) -> bool:
    first_a = _orientation(first_start, first_end, second_start)
    first_b = _orientation(first_start, first_end, second_end)
    second_a = _orientation(second_start, second_end, first_start)
    second_b = _orientation(second_start, second_end, first_end)
    if first_a == 0 and _point_on_segment(second_start, first_start, first_end):
        return True
    if first_b == 0 and _point_on_segment(second_end, first_start, first_end):
        return True
    if second_a == 0 and _point_on_segment(first_start, second_start, second_end):
        return True
    if second_b == 0 and _point_on_segment(first_end, second_start, second_end):
        return True
    return (first_a > 0) != (first_b > 0) and (second_a > 0) != (second_b > 0)


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


def _validate_program_order(programs: tuple[ProductionProgram, ...], issues: _Issues) -> None:
    if tuple(program.run_order for program in programs) != tuple(range(1, len(programs) + 1)):
        issues.add("PROGRAM_ORDER_INVALID", "program run order is not dense from one")
    identities = tuple(program.program_id for program in programs)
    if len(identities) != len(set(identities)):
        issues.add("PROGRAM_ORDER_INVALID", "program IDs are not unique")


def _validate_program_entry(
    program: ProductionProgram,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    context: ProductionExecutionContext,
    issues: _Issues,
) -> None:
    """Bind the CAM endpoint of the postprocessor's attested G53 return path.

    The machine-specific profile owns the outbound/tool-change/return clearance
    evidence.  CAM must still prove that the hand-off target is one exact,
    in-bounds setup-safe positioning move before any approach or cutting move.
    """

    if not program.moves:
        issues.add(
            "PROGRAM_ENTRY_INVALID",
            "program has no setup-safe entry target",
            program=program,
        )
        return
    entry = program.moves[0]
    expected_operation_id = program.operation_ids[0] if program.operation_ids else None
    if (
        entry.kind != ProductionMoveKind.RAPID
        or entry.role != ProductionMoveRole.POSITION
        or entry.pass_index != 1
        or entry.z_um != setup.safe_z_um
        or entry.operation_id != expected_operation_id
    ):
        issues.add(
            "PROGRAM_ENTRY_INVALID",
            "first move is not the exact setup-safe position hand-off",
            program=program,
            move=entry,
        )
    machine_x_um = setup.machine_wcs_origin.x_um + entry.x_um + binding.expected_length_offset_x_um
    machine_y_um = setup.machine_wcs_origin.y_um + entry.y_um + binding.expected_length_offset_y_um
    machine_z_um = setup.machine_wcs_z0_um + entry.z_um + binding.expected_length_offset_z_um
    if not (
        context.machine_x_min_um <= machine_x_um <= context.machine_x_max_um
        and context.machine_y_min_um <= machine_y_um <= context.machine_y_max_um
        and context.machine_z_min_um <= machine_z_um <= context.machine_z_max_um
    ):
        issues.add(
            "PROGRAM_ENTRY_INVALID",
            "setup-safe entry target leaves absolute machine bounds",
            program=program,
            move=entry,
        )


def _validate_program_partition(
    programs: tuple[ProductionProgram, ...],
    operations: tuple[CAMOperation, ...],
    source_setups: dict[str, Setup],
    bound_setups: dict[str, BoundSetup],
    bindings_by_source_tool: dict[str, ProductionToolBinding],
    recipes: tuple[CuttingRecipe, ...],
    context: ProductionExecutionContext,
    issues: _Issues,
) -> None:
    """Rebuild the deterministic setup/tool/RPM/release program partition."""

    groups: dict[tuple[str, str, int, bool], list[CAMOperation]] = {}
    for operation in operations:
        setup = bound_setups.get(operation.setup_id)
        binding = bindings_by_source_tool.get(operation.tool_id)
        if setup is None or binding is None:
            continue
        matches = tuple(
            recipe
            for recipe in recipes
            if (
                recipe.machine_profile_id == context.machine_profile_id
                and recipe.machine_profile_version == context.machine_profile_version
                and recipe.material_id == setup.material_id
                and recipe.material_version == setup.material_version
                and recipe.tool_id == binding.tool_id
                and recipe.tool_version == binding.tool_version
                and recipe.operation_kind == operation.kind
            )
        )
        if len(matches) != 1:
            continue
        key = (
            operation.setup_id,
            operation.tool_id,
            matches[0].spindle_rpm,
            _is_release_contour(operation),
        )
        groups.setdefault(key, []).append(operation)
    setup_order = {setup_id: index for index, setup_id in enumerate(source_setups)}
    expected_keys = tuple(
        sorted(
            groups,
            key=lambda key: (
                setup_order.get(key[0], len(setup_order)),
                key[3],
                key[1],
                key[2],
            ),
        )
    )
    if len(programs) != len(expected_keys):
        issues.add(
            "PROGRAM_PARTITION_INVALID",
            "program inventory differs from deterministic source setup/tool/RPM groups",
        )
    for run_order, (program, key) in enumerate(
        zip(programs, expected_keys, strict=False),
        start=1,
    ):
        setup_id, source_tool_id, spindle_rpm, release_phase = key
        binding = bindings_by_source_tool[source_tool_id]
        expected_operations = tuple(
            sorted(
                groups[key],
                key=lambda operation: (
                    operation.kind == OperationKind.CONTOUR,
                    operation.kind.value,
                    operation.operation_id,
                ),
            )
        )
        expected_operation_ids = tuple(operation.operation_id for operation in expected_operations)
        expected_program_id = f"program:{run_order:03d}:{setup_id}:{binding.tool_id}:S{spindle_rpm}"
        if (
            program.run_order != run_order
            or program.program_id != expected_program_id
            or program.setup_id != setup_id
            or program.tool_id != binding.tool_id
            or program.tool_version != binding.tool_version
            or program.operation_ids != expected_operation_ids
            or bool(program.release_operation_ids) is not release_phase
        ):
            issues.add(
                "PROGRAM_PARTITION_INVALID",
                "program identity/order/content differs from its deterministic source group",
                program=program,
            )


def _recipes_for_operations(
    program: ProductionProgram,
    operations: tuple[CAMOperation, ...],
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipes: tuple[CuttingRecipe, ...],
    machine_profile_id: str,
    machine_profile_version: str,
    issues: _Issues,
) -> dict[str, CuttingRecipe]:
    result: dict[str, CuttingRecipe] = {}
    for operation in operations:
        matches = tuple(
            recipe
            for recipe in recipes
            if (
                recipe.machine_profile_id == machine_profile_id
                and recipe.machine_profile_version == machine_profile_version
                and recipe.material_id == setup.material_id
                and recipe.material_version == setup.material_version
                and recipe.tool_id == binding.tool_id
                and recipe.tool_version == binding.tool_version
                and recipe.operation_kind == operation.kind
            )
        )
        if len(matches) != 1:
            issues.add(
                "PROGRAM_RECIPE_BINDING_INVALID",
                "operation does not resolve exactly one cutting recipe",
                program=program,
                operation_id=operation.operation_id,
            )
        else:
            result[operation.operation_id] = matches[0]
    return result


def _validate_operation_contract(
    program: ProductionProgram,
    operation: CAMOperation,
    setup: BoundSetup,
    source_tool: ToolSpec,
    binding: ProductionToolBinding,
    issues: _Issues,
) -> None:
    if operation.kind not in source_tool.supported_operations:
        issues.add(
            "SOURCE_TOOL_OPERATION_INVALID",
            "source tool snapshot does not support the operation kind",
            program=program,
            operation_id=operation.operation_id,
        )
    if (
        binding.source_tool_id != source_tool.tool_id
        or binding.source_tool_version != source_tool.version
    ):
        issues.add(
            "SOURCE_TOOL_BINDING_INVALID",
            "operation source tool does not match the production tool provenance",
            program=program,
            operation_id=operation.operation_id,
        )
    has_corner_contract = (
        operation.corner_strategy is not None
        or operation.corner_relief_radius_um is not None
        or bool(operation.open_end_reliefs)
    )
    if operation.kind == OperationKind.DRILL:
        if operation.through:
            issues.add(
                "THROUGH_DRILL_UNSUPPORTED",
                "through drilling lacks a bound tip/full-diameter breakthrough model",
                program=program,
                operation_id=operation.operation_id,
            )
        if (
            binding.geometry != ProductionToolGeometry.DRILL
            or not binding.center_cutting
            or type(binding.drill_point_length_um) is not int
            or binding.drill_point_length_um != 0
            or operation.diameter_um is None
            or operation.width_um is not None
            or operation.length_um is not None
            or any(
                value is not None
                for value in (
                    operation.cutter_envelope_x_um,
                    operation.cutter_envelope_y_um,
                    operation.cutter_envelope_width_um,
                    operation.cutter_envelope_length_um,
                )
            )
            or operation.compensation is not None
            or has_corner_contract
        ):
            issues.add(
                "OPERATION_CONTRACT_INVALID",
                "drill geometry is not exactly bound to the production drill",
                program=program,
                operation_id=operation.operation_id,
            )
        return
    if operation.kind in {OperationKind.POCKET, OperationKind.GROOVE}:
        if (
            binding.geometry != ProductionToolGeometry.FLAT_END_MILL
            or not binding.center_cutting
            or operation.width_um is None
            or operation.length_um is None
            or operation.diameter_um is not None
            or operation.through
            or operation.compensation is not None
            or (
                operation.width_um is not None
                and operation.width_um <= binding.effective_diameter_um
            )
            or (
                operation.length_um is not None
                and operation.length_um <= binding.effective_diameter_um
            )
        ):
            issues.add(
                "OPERATION_CONTRACT_INVALID",
                "area geometry is not machinable by the bound production tool",
                program=program,
                operation_id=operation.operation_id,
            )
        if has_corner_contract and (
            operation.corner_strategy not in {"dogbone-v1", "dogbone-v2"}
            or operation.corner_relief_radius_um != binding.effective_diameter_um // 2
        ):
            issues.add(
                "OPERATION_CONTRACT_INVALID",
                "dogbone geometry is not exactly bound to the production cutter radius",
                program=program,
                operation_id=operation.operation_id,
            )
        if operation.width_um is not None and operation.length_um is not None:
            nominal = Rect(
                operation.x_um,
                operation.y_um,
                operation.width_um,
                operation.length_um,
            )
            expected_envelope = _independent_area_cutter_envelope(
                operation,
                nominal,
                binding.effective_diameter_um // 2,
            )
            if _declared_cutter_envelope(operation) != expected_envelope:
                issues.add(
                    "OPERATION_CONTRACT_INVALID",
                    "area cutter envelope differs from independently reconstructed geometry",
                    program=program,
                    operation_id=operation.operation_id,
                )
        return
    if operation.kind == OperationKind.CONTOUR and (
        binding.geometry != ProductionToolGeometry.FLAT_END_MILL
        or not binding.center_cutting
        or operation.width_um is None
        or operation.length_um is None
        or operation.diameter_um is not None
        or has_corner_contract
        or operation.compensation not in {"INSIDE", "OUTSIDE"}
        or (operation.through and operation.compensation != "OUTSIDE")
        or (operation.width_um is not None and operation.width_um <= binding.effective_diameter_um)
        or (
            operation.length_um is not None and operation.length_um <= binding.effective_diameter_um
        )
    ):
        issues.add(
            "OPERATION_CONTRACT_INVALID",
            "contour geometry is not machinable by the bound production tool",
            program=program,
            operation_id=operation.operation_id,
        )
    elif operation.width_um is not None and operation.length_um is not None:
        nominal = Rect(
            operation.x_um,
            operation.y_um,
            operation.width_um,
            operation.length_um,
        )
        if _declared_cutter_envelope(operation) != nominal:
            issues.add(
                "OPERATION_CONTRACT_INVALID",
                "contour cutter envelope differs from its exact nominal geometry",
                program=program,
                operation_id=operation.operation_id,
            )


def _declared_cutter_envelope(operation: CAMOperation) -> Rect | None:
    values = (
        operation.cutter_envelope_x_um,
        operation.cutter_envelope_y_um,
        operation.cutter_envelope_width_um,
        operation.cutter_envelope_length_um,
    )
    if any(value is None for value in values):
        return None
    x_um, y_um, width_um, height_um = values
    assert x_um is not None
    assert y_um is not None
    assert width_um is not None
    assert height_um is not None
    return Rect(x_um, y_um, width_um, height_um)


def _independent_area_cutter_envelope(
    operation: CAMOperation,
    nominal: Rect,
    radius_um: int,
) -> Rect:
    centres = _independent_dogbone_centres(operation)
    if not centres:
        return nominal
    left = min(nominal.x_um, *(x_um - radius_um for x_um, _ in centres))
    right = max(nominal.right_um, *(x_um + radius_um for x_um, _ in centres))
    bottom = min(nominal.y_um, *(y_um - radius_um for _, y_um in centres))
    top = max(nominal.top_um, *(y_um + radius_um for _, y_um in centres))
    return Rect(left, bottom, right - left, top - bottom)


def _validate_recipe_operation_contract(
    program: ProductionProgram,
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    issues: _Issues,
) -> None:
    if (
        recipe.operation_kind != operation.kind
        or recipe.tool_id != binding.tool_id
        or recipe.tool_version != binding.tool_version
    ):
        issues.add(
            "PROGRAM_RECIPE_BINDING_INVALID",
            "cutting recipe differs from the operation or production tool",
            program=program,
            operation_id=operation.operation_id,
        )
    if recipe.approach_clearance_um >= setup.safe_z_um:
        issues.add(
            "RECIPE_CLEARANCE_INVALID",
            "recipe approach clearance reaches or exceeds setup safe Z",
            program=program,
            operation_id=operation.operation_id,
        )
    if recipe.process_accuracy_um >= setup.minimum_rapid_clearance_um:
        issues.add(
            "RECIPE_CLEARANCE_INVALID",
            "recipe uncertainty exhausts the setup minimum rapid clearance",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.tolerance_um and recipe.accepted_tolerance_um != operation.tolerance_um:
        issues.add(
            "RECIPE_TOLERANCE_INVALID",
            "recipe does not accept the operation tolerance exactly",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.fit_clearance_um and (
        2 * (recipe.accepted_tolerance_um + recipe.process_accuracy_um)
        >= operation.fit_clearance_um
    ):
        issues.add(
            "RECIPE_TOLERANCE_INVALID",
            "recipe uncertainty exhausts the operation fit-clearance budget",
            program=program,
            operation_id=operation.operation_id,
        )
    if (
        operation.kind == OperationKind.CONTOUR
        and operation.compensation == "OUTSIDE"
        and recipe.process_accuracy_um
        < _canonical_contour_interpolation_error_um(binding.effective_diameter_um // 2)
    ):
        issues.add(
            "CONTOUR_INTERPOLATION_ACCURACY_INVALID",
            "recipe accuracy omits canonical rounded-corner chord/rounding error",
            program=program,
            operation_id=operation.operation_id,
        )
    target_depth = _target_depth(operation, setup, recipe, program, issues)
    uncertainty_depth = target_depth + recipe.process_accuracy_um
    if uncertainty_depth > binding.cutting_length_um:
        issues.add(
            "TOOL_CUTTING_LENGTH_EXCEEDED",
            "operation target depth exceeds the verified cutting length",
            program=program,
            operation_id=operation.operation_id,
        )
    if uncertainty_depth + binding.minimum_holder_clearance_um > binding.measured_stickout_um:
        issues.add(
            "TOOL_HOLDER_CLEARANCE_EXCEEDED",
            "operation depth leaves less than the bound minimum holder clearance",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.kind == OperationKind.DRILL and (
        operation.diameter_um is None
        or abs(operation.diameter_um - binding.effective_diameter_um) > recipe.diameter_tolerance_um
    ):
        issues.add(
            "OPERATION_CONTRACT_INVALID",
            "drill diameter exceeds the recipe's production-tool tolerance",
            program=program,
            operation_id=operation.operation_id,
        )
    if (
        operation.kind == OperationKind.DRILL
        and operation.diameter_um is not None
        and abs(operation.diameter_um - binding.effective_diameter_um) + recipe.process_accuracy_um
        > recipe.accepted_tolerance_um
    ):
        issues.add(
            "DRILL_DIAMETER_BUDGET_INVALID",
            "drill/tool diameter mismatch plus process uncertainty exceeds tolerance",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.through and recipe.through_overtravel_um <= recipe.process_accuracy_um:
        issues.add(
            "THROUGH_CUT_UNCERTAINTY_INVALID",
            "through overtravel does not exceed worst-case process uncertainty",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.through and (
        recipe.through_overtravel_um <= 0
        or recipe.through_overtravel_um + recipe.process_accuracy_um
        > setup.through_cut_allowance_um
    ):
        issues.add(
            "THROUGH_CUT_ALLOWANCE_EXCEEDED",
            "recipe through overtravel exceeds the setup's bound spoilboard allowance",
            program=program,
            operation_id=operation.operation_id,
        )
    if not operation.through and uncertainty_depth >= setup.stock_thickness_um:
        issues.add(
            "NONTHROUGH_DEPTH_UNCERTAINTY_INVALID",
            "non-through operation uncertainty reaches the stock bottom",
            program=program,
            operation_id=operation.operation_id,
        )
    if _is_release_contour(operation) and recipe.tab_height_um <= recipe.process_accuracy_um:
        issues.add(
            "TAB_CONTRACT_INVALID",
            "recipe uncertainty consumes the holding-tab height",
            program=program,
            operation_id=operation.operation_id,
        )
    if _is_release_contour(operation) and recipe.tab_width_um <= 2 * recipe.process_accuracy_um:
        issues.add(
            "TAB_CONTRACT_INVALID",
            "recipe uncertainty consumes the holding-tab width",
            program=program,
            operation_id=operation.operation_id,
        )
    if operation.kind in {OperationKind.POCKET, OperationKind.GROOVE}:
        stepover_um = binding.effective_diameter_um * recipe.stepover_ppm // 1_000_000
        if stepover_um + 2 * recipe.process_accuracy_um > binding.effective_diameter_um:
            issues.add(
                "STEPOVER_COVERAGE_INVALID",
                "recipe stepover exceeds process-accuracy-adjusted cutter coverage",
                program=program,
                operation_id=operation.operation_id,
            )


def _validate_move_order(program: ProductionProgram, issues: _Issues) -> None:
    if tuple(move.sequence for move in program.moves) != tuple(range(1, len(program.moves) + 1)):
        issues.add(
            "MOVE_SEQUENCE_INVALID",
            "move sequence is not dense from one",
            program=program,
        )
    observed: list[str] = []
    for move in program.moves:
        if not observed or observed[-1] != move.operation_id:
            observed.append(move.operation_id)
    if tuple(observed) != program.operation_ids:
        issues.add(
            "MOVE_OPERATION_ORDER_INVALID",
            "move operation blocks differ from declared program order",
            program=program,
        )


def _build_program_envelopes(
    program: ProductionProgram,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipes_by_operation: dict[str, CuttingRecipe],
) -> tuple[SweptEnvelope, ...]:
    if not program.moves:
        return ()
    cutter_radius = (binding.effective_diameter_um + 1) // 2
    previous = _Point3D(
        program.moves[0].x_um,
        program.moves[0].y_um,
        setup.safe_z_um,
    )
    values: list[SweptEnvelope] = []
    for move in program.moves:
        recipe = recipes_by_operation.get(move.operation_id)
        process_accuracy = recipe.process_accuracy_um if recipe is not None else 0
        swept_radius = cutter_radius + process_accuracy
        assembly_radius = binding.assembly_collision_radius_um + process_accuracy
        material_removal = (
            move.kind == ProductionMoveKind.LINEAR and min(previous.z_um, move.z_um) < 0
        )
        endpoint_uncertainty = process_accuracy if material_removal else 0
        machine_x_min = (
            setup.machine_wcs_origin.x_um
            + min(previous.x_um, move.x_um)
            + binding.expected_length_offset_x_um
            - endpoint_uncertainty
        )
        machine_y_min = (
            setup.machine_wcs_origin.y_um
            + min(previous.y_um, move.y_um)
            + binding.expected_length_offset_y_um
            - endpoint_uncertainty
        )
        machine_z_min = (
            setup.machine_wcs_z0_um
            + min(previous.z_um, move.z_um)
            + binding.expected_length_offset_z_um
            - endpoint_uncertainty
        )
        machine_x_max = (
            setup.machine_wcs_origin.x_um
            + max(previous.x_um, move.x_um)
            + binding.expected_length_offset_x_um
            + endpoint_uncertainty
        )
        machine_y_max = (
            setup.machine_wcs_origin.y_um
            + max(previous.y_um, move.y_um)
            + binding.expected_length_offset_y_um
            + endpoint_uncertainty
        )
        machine_z_max = (
            setup.machine_wcs_z0_um
            + max(previous.z_um, move.z_um)
            + binding.expected_length_offset_z_um
            + endpoint_uncertainty
        )
        values.append(
            SweptEnvelope(
                program_id=program.program_id,
                setup_id=program.setup_id,
                tool_id=program.tool_id,
                operation_id=move.operation_id,
                move_sequence=move.sequence,
                motion_kind=move.kind.value,
                cutter_radius_um=cutter_radius,
                process_accuracy_um=process_accuracy,
                assembly_collision_radius_um=assembly_radius,
                x_min_um=min(previous.x_um, move.x_um) - swept_radius,
                y_min_um=min(previous.y_um, move.y_um) - swept_radius,
                z_min_um=min(previous.z_um, move.z_um),
                x_max_um=max(previous.x_um, move.x_um) + swept_radius,
                y_max_um=max(previous.y_um, move.y_um) + swept_radius,
                z_max_um=max(previous.z_um, move.z_um),
                machine_x_min_um=machine_x_min,
                machine_y_min_um=machine_y_min,
                machine_z_min_um=machine_z_min,
                machine_x_max_um=machine_x_max,
                machine_y_max_um=machine_y_max,
                machine_z_max_um=machine_z_max,
                assembly_x_min_um=min(previous.x_um, move.x_um) - assembly_radius,
                assembly_y_min_um=min(previous.y_um, move.y_um) - assembly_radius,
                assembly_x_max_um=max(previous.x_um, move.x_um) + assembly_radius,
                assembly_y_max_um=max(previous.y_um, move.y_um) + assembly_radius,
                material_removal=material_removal,
                material_z_min_um=min(previous.z_um, move.z_um) if material_removal else None,
                material_z_max_um=(
                    min(0, max(previous.z_um, move.z_um)) if material_removal else None
                ),
            )
        )
        previous = _Point3D(move.x_um, move.y_um, move.z_um)
    return tuple(values)


def _validate_motion_safety(
    program: ProductionProgram,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    envelopes: tuple[SweptEnvelope, ...],
    context: ProductionExecutionContext,
    issues: _Issues,
) -> None:
    if not program.moves:
        issues.add("PROGRAM_EMPTY", "program contains no moves", program=program)
        return
    if binding.effective_diameter_um % 2:
        issues.add(
            "TOOL_RADIUS_INVALID",
            "production CAM v1 requires an even effective tool diameter",
            program=program,
        )
    previous = _Point3D(
        program.moves[0].x_um,
        program.moves[0].y_um,
        setup.safe_z_um,
    )
    stock = Rect(0, 0, setup.stock_width_um, setup.stock_height_um)
    for move, envelope in zip(program.moves, envelopes, strict=True):
        if not (0 <= move.x_um <= setup.stock_width_um and 0 <= move.y_um <= setup.stock_height_um):
            issues.add(
                "MOVE_OUTSIDE_STOCK",
                "tool centre leaves stock bounds",
                program=program,
                move=move,
            )
        if not (
            context.machine_x_min_um
            <= envelope.machine_x_min_um
            <= envelope.machine_x_max_um
            <= context.machine_x_max_um
            and context.machine_y_min_um
            <= envelope.machine_y_min_um
            <= envelope.machine_y_max_um
            <= context.machine_y_max_um
        ):
            issues.add(
                "MOVE_OUTSIDE_MACHINE",
                "G43-transformed controlled-point path leaves absolute machine XY travel",
                program=program,
                move=move,
            )
        z_uncertainty = envelope.process_accuracy_um if envelope.material_removal else 0
        if (
            envelope.machine_z_min_um < context.machine_z_min_um
            or envelope.machine_z_max_um > context.machine_z_max_um
            or envelope.z_min_um - z_uncertainty
            < -(setup.stock_thickness_um + setup.through_cut_allowance_um)
        ):
            issues.add(
                "MOVE_Z_LIMIT",
                "move exceeds the G43-transformed machine or programmed stock Z limit",
                program=program,
                move=move,
            )
        if -min(envelope.z_min_um, 0) + z_uncertainty > binding.cutting_length_um:
            issues.add(
                "TOOL_CUTTING_LENGTH_EXCEEDED",
                "move depth exceeds the verified cutting length",
                program=program,
                move=move,
            )
        if (
            -min(envelope.z_min_um, 0) + z_uncertainty + binding.minimum_holder_clearance_um
            > binding.measured_stickout_um
        ):
            issues.add(
                "TOOL_HOLDER_CLEARANCE_EXCEEDED",
                "move depth leaves less than the bound minimum holder clearance",
                program=program,
                move=move,
            )
        xy_changed = previous.x_um != move.x_um or previous.y_um != move.y_um
        if move.kind == ProductionMoveKind.RAPID:
            if move.z_um < 0:
                issues.add(
                    "RAPID_BELOW_STOCK_TOP",
                    "rapid motion enters material",
                    program=program,
                    move=move,
                )
            if xy_changed and (previous.z_um != setup.safe_z_um or move.z_um != setup.safe_z_um):
                issues.add(
                    "RAPID_XY_BELOW_SAFE_Z",
                    "XY rapid does not remain at exact safe Z",
                    program=program,
                    move=move,
                )
        elif move.role not in {
            ProductionMoveRole.CUT,
            ProductionMoveRole.TAB_RAMP,
            ProductionMoveRole.TAB_BRIDGE,
        }:
            issues.add(
                "MOVE_ROLE_INVALID",
                "linear motion uses a non-cutting role",
                program=program,
                move=move,
            )
        if envelope.material_removal:
            footprint = Rect(
                envelope.x_min_um,
                envelope.y_min_um,
                envelope.x_max_um - envelope.x_min_um,
                envelope.y_max_um - envelope.y_min_um,
            )
            if not stock.contains(footprint):
                issues.add(
                    "CUTTER_SWEEP_OUTSIDE_STOCK",
                    "tool-radius-expanded cutting sweep leaves stock",
                    program=program,
                    move=move,
                )
            if any(_rectangles_touch_or_overlap(footprint, zone) for zone in setup.keep_out_zones):
                issues.add(
                    "KEEP_OUT_COLLISION",
                    "tool-radius-expanded cutting sweep reaches keep-out zone",
                    program=program,
                    move=move,
                )
        assembly_footprint = Rect(
            envelope.assembly_x_min_um,
            envelope.assembly_y_min_um,
            envelope.assembly_x_max_um - envelope.assembly_x_min_um,
            envelope.assembly_y_max_um - envelope.assembly_y_min_um,
        )
        if move.kind == ProductionMoveKind.LINEAR and any(
            _rectangles_touch_or_overlap(assembly_footprint, zone) for zone in setup.keep_out_zones
        ):
            issues.add(
                "TOOL_ASSEMBLY_KEEP_OUT_COLLISION",
                "tool-assembly-expanded linear sweep reaches a keep-out zone",
                program=program,
                move=move,
            )
        elif move.kind == ProductionMoveKind.RAPID and (
            previous.z_um < setup.safe_z_um or move.z_um < setup.safe_z_um
        ):
            footprint = Rect(
                envelope.assembly_x_min_um,
                envelope.assembly_y_min_um,
                envelope.assembly_x_max_um - envelope.assembly_x_min_um,
                envelope.assembly_y_max_um - envelope.assembly_y_min_um,
            )
            if any(_rectangles_touch_or_overlap(footprint, zone) for zone in setup.keep_out_zones):
                issues.add(
                    "TOOL_ASSEMBLY_KEEP_OUT_COLLISION",
                    "tool-assembly-expanded rapid below safe Z reaches a keep-out zone",
                    program=program,
                    move=move,
                )
        previous = _Point3D(move.x_um, move.y_um, move.z_um)


def _validate_operation_motion(
    program: ProductionProgram,
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    issues: _Issues,
) -> None:
    moves = tuple(move for move in program.moves if move.operation_id == operation.operation_id)
    if not moves:
        issues.add(
            "OPERATION_MOVE_COVERAGE_INVALID",
            "operation has no moves",
            program=program,
            operation_id=operation.operation_id,
        )
        return
    if moves[0].kind != ProductionMoveKind.RAPID or moves[0].role != ProductionMoveRole.POSITION:
        issues.add(
            "OPERATION_SEQUENCE_INVALID",
            "operation does not begin with a rapid position",
            program=program,
            operation_id=operation.operation_id,
        )
    if (
        moves[-1].kind != ProductionMoveKind.RAPID
        or moves[-1].role != ProductionMoveRole.RETRACT
        or moves[-1].z_um != setup.safe_z_um
    ):
        issues.add(
            "OPERATION_SEQUENCE_INVALID",
            "operation does not finish with an exact safe-Z retract",
            program=program,
            operation_id=operation.operation_id,
        )

    target_depth = _target_depth(operation, setup, recipe, program, issues)
    step = (
        recipe.peck_depth_um
        if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}
        else recipe.stepdown_um
    )
    expected_levels = _depth_levels(target_depth, step)
    observed_passes = tuple(dict.fromkeys(move.pass_index for move in moves))
    if observed_passes != tuple(range(1, len(expected_levels) + 1)):
        issues.add(
            "PASS_ORDER_INVALID",
            "operation pass indexes are not dense and canonical",
            program=program,
            operation_id=operation.operation_id,
        )
    for pass_index, expected_z in enumerate(expected_levels, start=1):
        cutting = tuple(
            move
            for move in moves
            if move.pass_index == pass_index and move.kind == ProductionMoveKind.LINEAR
        )
        if not cutting or min(move.z_um for move in cutting) != expected_z:
            issues.add(
                "CUT_DEPTH_INVALID",
                "pass does not reach its exact independently calculated depth",
                program=program,
                operation_id=operation.operation_id,
            )
        if any(move.z_um < expected_z or move.z_um >= 0 for move in cutting):
            issues.add(
                "CUT_DEPTH_INVALID",
                "cutting move exceeds its pass depth or safe envelope",
                program=program,
                operation_id=operation.operation_id,
            )
        tab_floor_z = -(setup.stock_thickness_um - recipe.tab_height_um)
        tabs_active = _is_release_contour(operation) and expected_z < tab_floor_z
        for move in cutting:
            allowed_z = {expected_z}
            if tabs_active and move.role in {
                ProductionMoveRole.TAB_RAMP,
                ProductionMoveRole.TAB_BRIDGE,
            }:
                allowed_z.add(tab_floor_z)
            if move.z_um not in allowed_z:
                issues.add(
                    "CUT_DEPTH_INVALID",
                    "cutting move does not use its pass or contracted tab depth",
                    program=program,
                    move=move,
                )

    previous: ProductionMove | None = None
    for move in moves:
        if move.kind == ProductionMoveKind.RAPID:
            if move.role in {ProductionMoveRole.POSITION, ProductionMoveRole.RETRACT}:
                expected_z = setup.safe_z_um
            else:
                expected_z = recipe.approach_clearance_um
            if move.z_um != expected_z:
                issues.add(
                    "RAPID_Z_INVALID",
                    "rapid role does not use its exact contracted Z",
                    program=program,
                    move=move,
                )
        else:
            plunge = (
                previous is not None
                and previous.x_um == move.x_um
                and previous.y_um == move.y_um
                and move.z_um != previous.z_um
            )
            expected_feed = recipe.plunge_um_min if plunge else recipe.feed_um_min
            if move.feed_um_min != expected_feed:
                issues.add(
                    "CUT_FEED_INVALID",
                    "linear feed differs from the exact cutting recipe",
                    program=program,
                    move=move,
                )
            if not _operation_point_allowed(operation, binding, move.x_um, move.y_um):
                issues.add(
                    "OPERATION_GEOMETRY_MISMATCH",
                    "cutting endpoint lies outside reconstructed operation geometry",
                    program=program,
                    move=move,
                )
            if (
                previous is not None
                and min(previous.z_um, move.z_um) < 0
                and not _material_removal_segment_allowed(
                    operation,
                    binding,
                    recipe,
                    previous,
                    move,
                )
            ):
                issues.add(
                    "MATERIAL_REMOVAL_SEGMENT_INVALID",
                    (
                        "material-removing 3D segment leaves the independently reconstructed "
                        "operation geometry or violates axial plunge semantics"
                    ),
                    program=program,
                    move=move,
                )
        previous = move

    _validate_pass_sequences(
        program,
        operation,
        setup,
        binding,
        recipe,
        moves,
        expected_levels,
        issues,
    )

    if operation.kind in {OperationKind.POCKET, OperationKind.GROOVE}:
        _validate_area_coverage(program, operation, binding, recipe, moves, expected_levels, issues)
    elif operation.kind == OperationKind.CONTOUR:
        _validate_contour_coverage(
            program,
            operation,
            binding,
            recipe,
            moves,
            expected_levels,
            issues,
        )
    else:
        for pass_index in range(1, len(expected_levels) + 1):
            cutting = tuple(
                move
                for move in moves
                if move.pass_index == pass_index and move.kind == ProductionMoveKind.LINEAR
            )
            if len(cutting) != 1 or any(
                (move.x_um, move.y_um) != (operation.x_um, operation.y_um) for move in cutting
            ):
                issues.add(
                    "MATERIAL_REMOVAL_COVERAGE_INVALID",
                    "drilling pass is not one exact axial cutting move",
                    program=program,
                    operation_id=operation.operation_id,
                )


def _validate_pass_sequences(
    program: ProductionProgram,
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    moves: tuple[ProductionMove, ...],
    expected_levels: tuple[int, ...],
    issues: _Issues,
) -> None:
    if operation.kind == OperationKind.DRILL:
        if len(moves) < 4:
            issues.add(
                "OPERATION_SEQUENCE_INVALID",
                "drill cycle is incomplete",
                program=program,
                operation_id=operation.operation_id,
            )
            return
        expected_prefix = (
            (ProductionMoveKind.RAPID, ProductionMoveRole.POSITION, setup.safe_z_um),
            (
                ProductionMoveKind.RAPID,
                ProductionMoveRole.APPROACH,
                recipe.approach_clearance_um,
            ),
        )
        if tuple((move.kind, move.role, move.z_um) for move in moves[:2]) != expected_prefix:
            issues.add(
                "OPERATION_SEQUENCE_INVALID",
                "drill cycle lacks its exact position and approach prefix",
                program=program,
                operation_id=operation.operation_id,
            )
        linear_indexes = tuple(
            index for index, move in enumerate(moves) if move.kind == ProductionMoveKind.LINEAR
        )
        if len(linear_indexes) != len(expected_levels):
            issues.add(
                "OPERATION_SEQUENCE_INVALID",
                "drill cycle does not contain one plunge per peck depth",
                program=program,
                operation_id=operation.operation_id,
            )
            return
        for pass_index, (linear_index, level) in enumerate(
            zip(linear_indexes, expected_levels, strict=True),
            start=1,
        ):
            move = moves[linear_index]
            if (
                move.pass_index != pass_index
                or move.role != ProductionMoveRole.CUT
                or move.z_um != level
                or (move.x_um, move.y_um) != (operation.x_um, operation.y_um)
            ):
                issues.add(
                    "OPERATION_SEQUENCE_INVALID",
                    "drill peck does not match its exact point, role, pass and depth",
                    program=program,
                    move=move,
                )
            if pass_index < len(expected_levels):
                if linear_index + 1 >= len(moves):
                    issues.add(
                        "OPERATION_SEQUENCE_INVALID",
                        "drill peck lacks its contracted retract",
                        program=program,
                        move=move,
                    )
                    continue
                retract = moves[linear_index + 1]
                if (
                    retract.kind != ProductionMoveKind.RAPID
                    or retract.role != ProductionMoveRole.PECK_RETRACT
                    or retract.pass_index != pass_index
                    or retract.z_um != recipe.approach_clearance_um
                    or (retract.x_um, retract.y_um) != (operation.x_um, operation.y_um)
                ):
                    issues.add(
                        "OPERATION_SEQUENCE_INVALID",
                        "drill peck retract differs from the exact recipe cycle",
                        program=program,
                        move=retract,
                    )
        return

    for pass_index in range(1, len(expected_levels) + 1):
        pass_moves = tuple(move for move in moves if move.pass_index == pass_index)
        if (
            len(pass_moves) < 4
            or pass_moves[0].kind != ProductionMoveKind.RAPID
            or pass_moves[0].role != ProductionMoveRole.POSITION
            or pass_moves[0].z_um != setup.safe_z_um
            or pass_moves[1].kind != ProductionMoveKind.RAPID
            or pass_moves[1].role != ProductionMoveRole.APPROACH
            or pass_moves[1].z_um != recipe.approach_clearance_um
            or pass_moves[-1].kind != ProductionMoveKind.RAPID
            or pass_moves[-1].role != ProductionMoveRole.RETRACT
            or pass_moves[-1].z_um != setup.safe_z_um
        ):
            issues.add(
                "OPERATION_SEQUENCE_INVALID",
                "area/contour pass lacks its exact position, approach or retract sequence",
                program=program,
                operation_id=operation.operation_id,
            )

    tab_moves = tuple(
        move
        for move in moves
        if move.role in {ProductionMoveRole.TAB_RAMP, ProductionMoveRole.TAB_BRIDGE}
    )
    if not _is_release_contour(operation):
        if tab_moves:
            issues.add(
                "TAB_CONTRACT_INVALID",
                "non-release operation contains tab motion",
                program=program,
                operation_id=operation.operation_id,
            )
        return

    tab_floor_z = -(setup.stock_thickness_um - recipe.tab_height_um)
    if recipe.tab_height_um <= 0 or tab_floor_z >= 0:
        issues.add(
            "TAB_CONTRACT_INVALID",
            "release contour tab height does not leave positive holding material",
            program=program,
            operation_id=operation.operation_id,
        )
        return
    expected_bridge_length = recipe.tab_width_um + binding.effective_diameter_um
    expected_bridges = _expected_tab_bridge_endpoints(
        operation,
        binding.effective_diameter_um // 2,
        expected_bridge_length,
    )
    index_by_sequence = {move.sequence: index for index, move in enumerate(moves)}
    for pass_index, level in enumerate(expected_levels, start=1):
        active = level < tab_floor_z
        pass_tab_moves = tuple(move for move in tab_moves if move.pass_index == pass_index)
        if not active:
            if pass_tab_moves:
                issues.add(
                    "TAB_CONTRACT_INVALID",
                    "release contour contains tab motion above the contracted tab floor",
                    program=program,
                    operation_id=operation.operation_id,
                )
            continue
        bridges = tuple(
            move for move in pass_tab_moves if move.role == ProductionMoveRole.TAB_BRIDGE
        )
        ramps = tuple(move for move in pass_tab_moves if move.role == ProductionMoveRole.TAB_RAMP)
        if len(bridges) != 4 or len(ramps) != 8:
            issues.add(
                "TAB_CONTRACT_INVALID",
                "release contour must retain four exact tab bridges per active pass",
                program=program,
                operation_id=operation.operation_id,
            )
        observed_bridges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        valid_structure = True
        for bridge in bridges:
            index = index_by_sequence[bridge.sequence]
            before = moves[index - 2] if index >= 2 else None
            ramp_up = moves[index - 1] if index >= 1 else None
            ramp_down = moves[index + 1] if index + 1 < len(moves) else None
            after = moves[index + 2] if index + 2 < len(moves) else None
            if ramp_up is not None:
                observed_bridges.append(
                    (
                        (ramp_up.x_um, ramp_up.y_um),
                        (bridge.x_um, bridge.y_um),
                    )
                )
            valid_structure = valid_structure and (
                before is not None
                and ramp_up is not None
                and ramp_down is not None
                and after is not None
                and before.kind == ProductionMoveKind.LINEAR
                and before.role == ProductionMoveRole.CUT
                and before.pass_index == pass_index
                and before.z_um == level
                and ramp_up.kind == ProductionMoveKind.LINEAR
                and ramp_up.role == ProductionMoveRole.TAB_RAMP
                and ramp_up.pass_index == pass_index
                and (before.x_um, before.y_um) == (ramp_up.x_um, ramp_up.y_um)
                and ramp_up.z_um == tab_floor_z
                and bridge.kind == ProductionMoveKind.LINEAR
                and bridge.role == ProductionMoveRole.TAB_BRIDGE
                and bridge.pass_index == pass_index
                and bridge.z_um == tab_floor_z
                and ramp_down.kind == ProductionMoveKind.LINEAR
                and ramp_down.role == ProductionMoveRole.TAB_RAMP
                and ramp_down.pass_index == pass_index
                and (ramp_down.x_um, ramp_down.y_um) == (bridge.x_um, bridge.y_um)
                and ramp_down.z_um == level
                and after.kind == ProductionMoveKind.LINEAR
                and after.role == ProductionMoveRole.CUT
                and after.pass_index == pass_index
                and after.z_um == level
            )
        if tuple(observed_bridges) != expected_bridges or not valid_structure:
            issues.add(
                "TAB_CONTRACT_INVALID",
                "tabs must be one exact centred physical-width bridge on each straight edge",
                program=program,
                operation_id=operation.operation_id,
            )


def _expected_tab_bridge_endpoints(
    operation: CAMOperation,
    radius_um: int,
    centreline_width_um: int,
) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    assert operation.width_um is not None
    assert operation.length_um is not None
    left = operation.x_um
    right = operation.x_um + operation.width_um
    bottom = operation.y_um
    top = operation.y_um + operation.length_um
    horizontal_before = (operation.width_um - centreline_width_um) // 2
    vertical_before = (operation.length_um - centreline_width_um) // 2
    return (
        (
            (left + horizontal_before, bottom - radius_um),
            (left + horizontal_before + centreline_width_um, bottom - radius_um),
        ),
        (
            (right + radius_um, bottom + vertical_before),
            (right + radius_um, bottom + vertical_before + centreline_width_um),
        ),
        (
            (right - horizontal_before, top + radius_um),
            (right - horizontal_before - centreline_width_um, top + radius_um),
        ),
        (
            (left - radius_um, top - vertical_before),
            (left - radius_um, top - vertical_before - centreline_width_um),
        ),
    )


def _target_depth(
    operation: CAMOperation,
    setup: BoundSetup,
    recipe: CuttingRecipe,
    program: ProductionProgram,
    issues: _Issues,
) -> int:
    if operation.depth_um <= 0:
        issues.add(
            "SOURCE_DEPTH_INVALID",
            "source operation depth is not positive",
            program=program,
            operation_id=operation.operation_id,
        )
        return 1
    if operation.through:
        target = setup.stock_thickness_um + recipe.through_overtravel_um
        if not (
            setup.stock_thickness_um
            <= operation.depth_um
            <= setup.stock_thickness_um + MAX_THROUGH_OVERTRAVEL_UM
        ):
            issues.add(
                "SOURCE_DEPTH_INVALID",
                "source through depth is outside the stock/overtravel envelope",
                program=program,
                operation_id=operation.operation_id,
            )
        if not (1 <= recipe.through_overtravel_um <= MAX_THROUGH_OVERTRAVEL_UM):
            issues.add(
                "SOURCE_DEPTH_INVALID",
                "through recipe overtravel is outside the supported envelope",
                program=program,
                operation_id=operation.operation_id,
            )
        return target
    if operation.depth_um >= setup.stock_thickness_um:
        issues.add(
            "SOURCE_DEPTH_INVALID",
            "non-through source operation reaches stock bottom",
            program=program,
            operation_id=operation.operation_id,
        )
    return operation.depth_um


def _depth_levels(depth_um: int, step_um: int) -> tuple[int, ...]:
    if depth_um <= 0 or step_um <= 0:
        return ()
    return tuple(-min(value, depth_um) for value in range(step_um, depth_um + step_um, step_um))


def _operation_point_allowed(
    operation: CAMOperation,
    binding: ProductionToolBinding,
    x_um: int,
    y_um: int,
) -> bool:
    radius = binding.effective_diameter_um // 2
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        return (x_um, y_um) == (operation.x_um, operation.y_um)
    if operation.width_um is None or operation.length_um is None:
        return False
    left = operation.x_um
    right = operation.x_um + operation.width_um
    bottom = operation.y_um
    top = operation.y_um + operation.length_um
    if operation.kind in {OperationKind.POCKET, OperationKind.GROOVE}:
        within_raster = (
            left + radius <= x_um <= right - radius and bottom + radius <= y_um <= top - radius
        )
        return within_raster or (x_um, y_um) in _independent_dogbone_centres(operation)
    if operation.kind != OperationKind.CONTOUR:
        return False
    if operation.compensation == "INSIDE":
        inset_left, inset_right = left + radius, right - radius
        inset_bottom, inset_top = bottom + radius, top - radius
        return (
            inset_left <= x_um <= inset_right
            and inset_bottom <= y_um <= inset_top
            and (x_um in {inset_left, inset_right} or y_um in {inset_bottom, inset_top})
        )
    if operation.compensation != "OUTSIDE":
        return False
    if (left <= x_um <= right and y_um in {bottom - radius, top + radius}) or (
        bottom <= y_um <= top and x_um in {left - radius, right + radius}
    ):
        return True
    corner_x = left if x_um < left else right if x_um > right else None
    corner_y = bottom if y_um < bottom else top if y_um > top else None
    if corner_x is None or corner_y is None:
        return False
    distance_squared = (x_um - corner_x) ** 2 + (y_um - corner_y) ** 2
    radius_squared = radius**2
    tolerance = 2 * radius * _ARC_RADIAL_TOLERANCE_UM + _ARC_RADIAL_TOLERANCE_UM**2
    return abs(distance_squared - radius_squared) <= tolerance


def _material_removal_segment_allowed(
    operation: CAMOperation,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    previous: ProductionMove,
    move: ProductionMove,
) -> bool:
    """Validate the complete material-removing segment, not only its endpoint.

    Production CAM v1 contracts axial PLUNGE entry and vertical tab ramps.  A
    segment that changes XY while crossing Z therefore cannot be part of any
    declared removal union.  Constant-Z cutting is checked against an
    independently reconstructed convex raster region, exact dogbone cycles, or
    the contracted contour primitives.
    """

    start = (previous.x_um, previous.y_um)
    end = (move.x_um, move.y_um)
    if start != end and previous.z_um != move.z_um:
        return False
    if start == end:
        return _operation_point_allowed(operation, binding, *start)
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        return False
    if operation.width_um is None or operation.length_um is None:
        return False

    radius_um = binding.effective_diameter_um // 2
    left = operation.x_um
    right = operation.x_um + operation.width_um
    bottom = operation.y_um
    top = operation.y_um + operation.length_um
    if operation.kind in {OperationKind.POCKET, OperationKind.GROOVE}:
        raster_left = left + radius_um
        raster_right = right - radius_um
        raster_bottom = bottom + radius_um
        raster_top = top - radius_um

        def in_raster(point: tuple[int, int]) -> bool:
            return (
                raster_left <= point[0] <= raster_right and raster_bottom <= point[1] <= raster_top
            )

        # The raster-centre rectangle is convex, so both endpoints inside it
        # prove the whole cutter-centre segment stays in the nominal area.  An
        # independently reconstructed dogbone is an exact axial cycle and may
        # never be connected laterally to the raster or another relief.
        return in_raster(start) and in_raster(end)
    if operation.kind != OperationKind.CONTOUR:
        return False
    if operation.compensation == "INSIDE":
        inset_left = left + radius_um
        inset_right = right - radius_um
        inset_bottom = bottom + radius_um
        inset_top = top - radius_um
        return _inside_contour_segment_allowed(
            start,
            end,
            left=inset_left,
            right=inset_right,
            bottom=inset_bottom,
            top=inset_top,
        )
    if operation.compensation != "OUTSIDE":
        return False
    return bool(
        _outside_segment_phases(
            start,
            end,
            left=left,
            right=right,
            bottom=bottom,
            top=top,
            radius_um=radius_um,
            process_accuracy_um=recipe.process_accuracy_um,
        )
    )


def _inside_contour_segment_allowed(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    left: int,
    right: int,
    bottom: int,
    top: int,
) -> bool:
    return (
        (start[0] == end[0] == left and bottom <= start[1] <= top and bottom <= end[1] <= top)
        or (start[0] == end[0] == right and bottom <= start[1] <= top and bottom <= end[1] <= top)
        or (start[1] == end[1] == bottom and left <= start[0] <= right and left <= end[0] <= right)
        or (start[1] == end[1] == top and left <= start[0] <= right and left <= end[0] <= right)
    )


def _independent_dogbone_centres(operation: CAMOperation) -> frozenset[tuple[int, int]]:
    if operation.corner_strategy not in {"dogbone-v1", "dogbone-v2"}:
        return frozenset()
    if operation.width_um is None or operation.length_um is None:
        return frozenset()
    declared = set(operation.open_end_reliefs)
    values: set[tuple[int, int]] = set()
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
        if operation.source_rotation_90:
            x_max = v_boundary == "v_min"
            y_max = u_boundary == "u_max"
        else:
            x_max = u_boundary == "u_max"
            y_max = v_boundary == "v_max"
        if operation.side.value == "B":
            y_max = not y_max
        values.add(
            (
                operation.x_um + (operation.width_um if x_max else 0),
                operation.y_um + (operation.length_um if y_max else 0),
            )
        )
    return frozenset(values)


def _validate_area_coverage(
    program: ProductionProgram,
    operation: CAMOperation,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    moves: tuple[ProductionMove, ...],
    expected_levels: tuple[int, ...],
    issues: _Issues,
) -> None:
    assert operation.width_um is not None
    assert operation.length_um is not None
    radius = binding.effective_diameter_um // 2
    x_min, x_max = operation.x_um + radius, operation.x_um + operation.width_um - radius
    y_min, y_max = operation.y_um + radius, operation.y_um + operation.length_um - radius
    stepover = binding.effective_diameter_um * recipe.stepover_ppm // 1_000_000
    guaranteed_cut_width = binding.effective_diameter_um - 2 * recipe.process_accuracy_um
    horizontal = operation.width_um - binding.effective_diameter_um >= (
        operation.length_um - binding.effective_diameter_um
    )
    previous_by_sequence = {move.sequence + 1: move for move in moves}
    for pass_index, level in enumerate(expected_levels, start=1):
        lanes: set[int] = set()
        for move in moves:
            previous = previous_by_sequence.get(move.sequence)
            if (
                previous is None
                or move.kind != ProductionMoveKind.LINEAR
                or previous.kind != ProductionMoveKind.LINEAR
                or move.pass_index != pass_index
                or previous.pass_index != pass_index
                or move.z_um != level
                or previous.z_um != level
            ):
                continue
            if (
                horizontal
                and move.y_um == previous.y_um
                and {
                    move.x_um,
                    previous.x_um,
                }
                == {x_min, x_max}
            ):
                lanes.add(move.y_um)
            if (
                not horizontal
                and move.x_um == previous.x_um
                and {
                    move.y_um,
                    previous.y_um,
                }
                == {y_min, y_max}
            ):
                lanes.add(move.x_um)
        expected_min, expected_max = (y_min, y_max) if horizontal else (x_min, x_max)
        ordered = sorted(lanes)
        if (
            not ordered
            or ordered[0] != expected_min
            or ordered[-1] != expected_max
            or any(
                right - left > min(stepover, guaranteed_cut_width)
                for left, right in zip(ordered, ordered[1:], strict=False)
            )
        ):
            issues.add(
                "MATERIAL_REMOVAL_COVERAGE_INVALID",
                "raster lanes do not conservatively cover the operation rectangle",
                program=program,
                operation_id=operation.operation_id,
            )
        expected_reliefs = _independent_dogbone_centres(operation)
        relief_cycles_valid = True
        for centre in expected_reliefs:
            matching_indexes = tuple(
                index
                for index, move in enumerate(moves)
                if move.pass_index == pass_index
                and move.kind == ProductionMoveKind.LINEAR
                and (move.x_um, move.y_um) == centre
            )
            if len(matching_indexes) != 1:
                relief_cycles_valid = False
                continue
            index = matching_indexes[0]
            previous = moves[index - 1] if index else None
            following = moves[index + 1] if index + 1 < len(moves) else None
            cut = moves[index]
            if not (
                previous is not None
                and following is not None
                and previous.kind == ProductionMoveKind.RAPID
                and previous.role == ProductionMoveRole.APPROACH
                and (previous.x_um, previous.y_um) == centre
                and cut.role == ProductionMoveRole.CUT
                and cut.z_um == level
                and following.kind == ProductionMoveKind.RAPID
                and following.role == ProductionMoveRole.RETRACT
                and (following.x_um, following.y_um) == centre
            ):
                relief_cycles_valid = False
        if not relief_cycles_valid:
            issues.add(
                "MATERIAL_REMOVAL_COVERAGE_INVALID",
                "dogbone relief centres do not each have one exact axial cycle per pass",
                program=program,
                operation_id=operation.operation_id,
            )


def _validate_contour_coverage(
    program: ProductionProgram,
    operation: CAMOperation,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
    moves: tuple[ProductionMove, ...],
    expected_levels: tuple[int, ...],
    issues: _Issues,
) -> None:
    assert operation.width_um is not None
    assert operation.length_um is not None
    radius = binding.effective_diameter_um // 2
    for pass_index, level in enumerate(expected_levels, start=1):
        pass_moves = tuple(move for move in moves if move.pass_index == pass_index)
        cutting = tuple(
            move for move in pass_moves if move.kind == ProductionMoveKind.LINEAR and move.z_um <= 0
        )
        points = tuple((move.x_um, move.y_um) for move in cutting)
        continuous_cutting_block = (
            len(pass_moves) >= 4
            and all(move.kind == ProductionMoveKind.LINEAR for move in pass_moves[2:-1])
            and tuple(pass_moves[2:-1]) == cutting
        )
        if operation.compensation == "INSIDE":
            left = operation.x_um + radius
            right = operation.x_um + operation.width_um - radius
            bottom = operation.y_um + radius
            top = operation.y_um + operation.length_um - radius
            expected_points = (
                (left, bottom),
                (left, top),
                (right, top),
                (right, bottom),
                (left, bottom),
            )
            valid = continuous_cutting_block and points == expected_points
        else:
            repeated_xy_is_only_vertical_tab_motion = all(
                first.x_um != second.x_um
                or first.y_um != second.y_um
                or (second.role == ProductionMoveRole.TAB_RAMP and first.z_um != second.z_um)
                for first, second in zip(cutting, cutting[1:], strict=False)
            )
            valid = (
                continuous_cutting_block
                and repeated_xy_is_only_vertical_tab_motion
                and _outside_contour_is_one_safe_lap(
                    points,
                    operation,
                    radius,
                    recipe.process_accuracy_um,
                )
            )
        if not valid or not cutting or min(move.z_um for move in cutting) != level:
            issues.add(
                "MATERIAL_REMOVAL_COVERAGE_INVALID",
                "contour pass is not one continuous independently reconstructed lap",
                program=program,
                operation_id=operation.operation_id,
            )


def _outside_contour_is_one_safe_lap(
    points: tuple[tuple[int, int], ...],
    operation: CAMOperation,
    radius_um: int,
    process_accuracy_um: int,
) -> bool:
    """Validate ordered rounded-rectangle traversal without generator helpers."""

    assert operation.width_um is not None
    assert operation.length_um is not None
    left = operation.x_um
    right = operation.x_um + operation.width_um
    bottom = operation.y_um
    top = operation.y_um + operation.length_um
    start = (left, bottom - radius_um)
    if len(points) < 13 or points[0] != start or points[-1] != start:
        return False
    if (
        min(x for x, _ in points) != left - radius_um
        or max(x for x, _ in points) != right + radius_um
        or min(y for _, y in points) != bottom - radius_um
        or max(y for _, y in points) != top + radius_um
    ):
        return False

    phases: list[int] = []
    current_phase = 0
    for first, second in zip(points, points[1:], strict=False):
        if first == second:
            # A repeated XY coordinate is valid only for a separately checked
            # vertical tab ramp; never let it stand in for perimeter coverage.
            continue
        candidates = _outside_segment_phases(
            first,
            second,
            left=left,
            right=right,
            bottom=bottom,
            top=top,
            radius_um=radius_um,
            process_accuracy_um=process_accuracy_um,
        )
        selected = next((phase for phase in candidates if phase >= current_phase), None)
        if selected is None or selected > current_phase + 1:
            return False
        current_phase = selected
        phases.append(selected)
    return bool(phases) and phases[0] == 0 and phases[-1] == 7 and set(phases) == set(range(8))


def _outside_segment_phases(
    first: tuple[int, int],
    second: tuple[int, int],
    *,
    left: int,
    right: int,
    bottom: int,
    top: int,
    radius_um: int,
    process_accuracy_um: int,
) -> tuple[int, ...]:
    first_x, first_y = first
    second_x, second_y = second
    phases: list[int] = []
    if first_y == second_y == bottom - radius_um and left <= first_x <= second_x <= right:
        phases.append(0)
    if _corner_chord_is_valid(
        first,
        second,
        centre=(right, bottom),
        radius_um=radius_um,
        x_sign=1,
        y_sign=-1,
        x_direction=1,
        y_direction=1,
        process_accuracy_um=process_accuracy_um,
    ):
        phases.append(1)
    if first_x == second_x == right + radius_um and bottom <= first_y <= second_y <= top:
        phases.append(2)
    if _corner_chord_is_valid(
        first,
        second,
        centre=(right, top),
        radius_um=radius_um,
        x_sign=1,
        y_sign=1,
        x_direction=-1,
        y_direction=1,
        process_accuracy_um=process_accuracy_um,
    ):
        phases.append(3)
    if first_y == second_y == top + radius_um and left <= second_x <= first_x <= right:
        phases.append(4)
    if _corner_chord_is_valid(
        first,
        second,
        centre=(left, top),
        radius_um=radius_um,
        x_sign=-1,
        y_sign=1,
        x_direction=-1,
        y_direction=-1,
        process_accuracy_um=process_accuracy_um,
    ):
        phases.append(5)
    if first_x == second_x == left - radius_um and bottom <= second_y <= first_y <= top:
        phases.append(6)
    if _corner_chord_is_valid(
        first,
        second,
        centre=(left, bottom),
        radius_um=radius_um,
        x_sign=-1,
        y_sign=-1,
        x_direction=1,
        y_direction=-1,
        process_accuracy_um=process_accuracy_um,
    ):
        phases.append(7)
    return tuple(phases)


def _corner_chord_is_valid(
    first: tuple[int, int],
    second: tuple[int, int],
    *,
    centre: tuple[int, int],
    radius_um: int,
    x_sign: int,
    y_sign: int,
    x_direction: int,
    y_direction: int,
    process_accuracy_um: int,
) -> bool:
    centre_x, centre_y = centre
    first_dx, first_dy = first[0] - centre_x, first[1] - centre_y
    second_dx, second_dy = second[0] - centre_x, second[1] - centre_y
    if (
        first_dx * x_sign < 0
        or second_dx * x_sign < 0
        or first_dy * y_sign < 0
        or second_dy * y_sign < 0
        or (second[0] - first[0]) * x_direction < 0
        or (second[1] - first[1]) * y_direction < 0
    ):
        return False
    radial_tolerance = 2 * radius_um * _ARC_RADIAL_TOLERANCE_UM + _ARC_RADIAL_TOLERANCE_UM**2
    if any(
        abs(dx * dx + dy * dy - radius_um * radius_um) > radial_tolerance
        for dx, dy in ((first_dx, first_dy), (second_dx, second_dy))
    ):
        return False
    chord_dx = second[0] - first[0]
    chord_dy = second[1] - first[1]
    maximum_axis_delta = (
        radius_um * _MAX_CORNER_CHORD_PPM + 999_999
    ) // 1_000_000 + 2 * _ARC_RADIAL_TOLERANCE_UM
    if max(abs(chord_dx), abs(chord_dy)) > maximum_axis_delta:
        return False
    midpoint_radius_twice = isqrt((first_dx + second_dx) ** 2 + (first_dy + second_dy) ** 2)
    radial_inset_um = max(0, 2 * radius_um - midpoint_radius_twice + 1) // 2
    return radial_inset_um <= process_accuracy_um


def _canonical_contour_interpolation_error_um(radius_um: int) -> int:
    """Independently bound integer rounding and sagitta for CAM v1 corners."""

    if radius_um <= 0:
        return radius_um

    def quarter_point(
        cosine_ppm: int,
        sine_ppm: int,
        quadrant: int,
    ) -> tuple[int, int]:
        if quadrant == 0:
            x_ppm, y_ppm = sine_ppm, -cosine_ppm
        elif quadrant == 1:
            x_ppm, y_ppm = cosine_ppm, sine_ppm
        elif quadrant == 2:
            x_ppm, y_ppm = -sine_ppm, cosine_ppm
        else:
            x_ppm, y_ppm = -cosine_ppm, -sine_ppm
        return (
            radius_um * x_ppm // 1_000_000,
            radius_um * y_ppm // 1_000_000,
        )

    maximum_error_um = 0
    for quadrant in range(4):
        points: list[tuple[int, int]] = []
        for cosine_ppm, sine_ppm in _CANONICAL_QUARTER_ARC_PPM:
            point = quarter_point(cosine_ppm, sine_ppm, quadrant)
            if not points or point != points[-1]:
                points.append(point)
        for first, second in zip(points, points[1:], strict=False):
            first_radius_squared = first[0] ** 2 + first[1] ** 2
            second_radius_squared = second[0] ** 2 + second[1] ** 2
            maximum_radius_squared = max(
                first_radius_squared,
                second_radius_squared,
            )
            maximum_radius_floor = isqrt(maximum_radius_squared)
            maximum_radius_ceil = maximum_radius_floor + int(
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


def _is_release_contour(operation: CAMOperation) -> bool:
    return (
        operation.kind == OperationKind.CONTOUR
        and operation.through
        and operation.compensation == "OUTSIDE"
    )


def _rectangles_touch_or_overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.right_um < second.x_um
        or second.right_um < first.x_um
        or first.top_um < second.y_um
        or second.top_um < first.y_um
    )


def _is_canonical_identity(value: str) -> bool:
    return (
        1 <= len(value) <= 256
        and value[0].isalnum()
        and all(character in _IDENTITY_CHARACTERS for character in value)
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_resolved_evidence_identity(value: str) -> bool:
    upper = value.upper()
    return _is_canonical_identity(value) and not any(
        token in upper for token in _UNRESOLVED_EVIDENCE_TOKENS
    )
