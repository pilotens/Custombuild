from __future__ import annotations

import copy
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from custombuild_cam import (
    CAMValidationError,
    build_validation_backplot,
    parse_operations_json,
    require_valid_operations,
    theoretical_removal_envelopes,
    validate_operations_document,
)
from custombuild_cam.model import ValidationBackplot
from custombuild_manufacturing import (
    DeterministicNester,
    FeatureKind,
    ManufacturingFeature,
    OperationKind,
    OperationsDocument,
    PartSpec,
    Point2D,
    Rect,
    Side,
    StockSheet,
    generate_operations_document,
    linuxcnc_reference_router_1325,
)
from custombuild_manufacturing.profiles import tool_catalog_fingerprint
from custombuild_postprocessors import (
    GCodeParseError,
    GCodeSafetyError,
    parse_gcode,
    validate_validation_program,
)
from custombuild_postprocessors.model import MachineProgram


def valid_document() -> OperationsDocument:
    feature = ManufacturingFeature(
        "hole",
        "panel",
        FeatureKind.DRILL,
        Side.A,
        50_000,
        50_000,
        10_000,
        diameter_um=8_000,
    )
    panel = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=(feature,),
        grain_direction="NONE",
    )
    stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), stock)
    return generate_operations_document(
        design_hash="f" * 64,
        parts=(panel,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
    )


def test_cam_validator_reports_document_and_operation_invariants() -> None:
    document = valid_document()
    setup = document.setups[0]
    operation = document.operations[0]
    mutated = copy.copy(document)
    object.__setattr__(mutated, "mode", "PRODUCTION")
    object.__setattr__(mutated, "design_hash", "")
    object.__setattr__(mutated, "setups", (setup, setup))
    invalid_operation = replace(
        operation,
        side=Side.B,
        tool_id="missing",
        depth_um=0,
        x_um=-1,
        y_um=-1,
        diameter_um=None,
    )
    outside_operation = replace(
        operation,
        operation_id="outside",
        x_um=setup.stock_width_um,
        y_um=setup.stock_height_um,
    )
    missing_setup = replace(operation, operation_id="orphan", setup_id="unknown")
    object.__setattr__(
        mutated,
        "operations",
        (invalid_operation, invalid_operation, outside_operation, missing_setup),
    )

    result = validate_operations_document(mutated)

    assert result.valid is False
    assert any("duplicate setup_id" in error for error in result.errors)
    assert any("duplicate operation_id" in error for error in result.errors)
    assert any("unknown setup" in error for error in result.errors)
    assert any("side mismatch" in error for error in result.errors)
    assert any("missing diameter" in error for error in result.errors)
    assert any("outside stock" in error for error in result.errors)
    with pytest.raises(CAMValidationError):
        require_valid_operations(mutated)
    with pytest.raises(CAMValidationError):
        build_validation_backplot(mutated)


def test_area_operation_validation_and_removal_envelope_branches() -> None:
    document = valid_document()
    operation = document.operations[0]
    missing_extent = replace(
        operation,
        operation_id="missing-extent",
        kind=OperationKind.POCKET,
        diameter_um=None,
        width_um=None,
        length_um=None,
        cutter_envelope_x_um=100_000,
        cutter_envelope_y_um=100_000,
        cutter_envelope_width_um=20_000,
        cutter_envelope_length_um=30_000,
    )
    outside = replace(
        missing_extent,
        operation_id="area-outside",
        x_um=990_000,
        y_um=590_000,
        width_um=20_000,
        length_um=20_000,
        cutter_envelope_x_um=990_000,
        cutter_envelope_y_um=590_000,
        cutter_envelope_width_um=20_000,
        cutter_envelope_length_um=20_000,
    )
    invalid = replace(document, operations=(missing_extent, outside))

    result = validate_operations_document(invalid)

    assert any("missing extent" in error for error in result.errors)
    assert any("envelope outside stock" in error for error in result.errors)

    valid_area = replace(
        missing_extent,
        operation_id=operation.operation_id,
        x_um=100_000,
        y_um=100_000,
        width_um=20_000,
        length_um=30_000,
        cutter_envelope_x_um=100_000,
        cutter_envelope_y_um=100_000,
        cutter_envelope_width_um=20_000,
        cutter_envelope_length_um=30_000,
    )
    area_machine = linuxcnc_reference_router_1325()
    area_tool = next(
        tool for tool in area_machine.tools if OperationKind.POCKET in tool.supported_operations
    )
    valid_area = replace(valid_area, tool_id=area_tool.tool_id)
    area_document = replace(
        document,
        setups=(replace(document.setups[0], tool_ids=(area_tool.tool_id,)),),
        operations=(valid_area,),
        tools=(area_tool,),
        tool_catalog_fingerprint=tool_catalog_fingerprint((area_tool,)),
    )
    envelope = theoretical_removal_envelopes(area_document, machine=area_machine)[0]
    assert (envelope.x_max_um, envelope.y_max_um) == (120_000, 130_000)
    assert len(build_validation_backplot(area_document, machine=area_machine).moves) == 6


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"\xff", "invalid UTF-8"),
        ("[]", "JSON object"),
        ('{"schema_version":"wrong"}', "schema_version"),
        ('{"schema_version":"custombuild.operations.v2","mode":"PRODUCTION"}', "validation-only"),
        (
            '{"schema_version":"custombuild.operations.v2","mode":"VALIDATION"}',
            "missing design_hash",
        ),
        (
            json.dumps(
                {
                    "schema_version": "custombuild.operations.v2",
                    "mode": "VALIDATION",
                    "design_hash": "x",
                    "machine_profile_id": "m",
                    "machine_profile_version": "1",
                    "tool_catalog_version": "tools-1",
                    "tool_catalog_fingerprint": "a" * 64,
                    "tools": [],
                    "setups": {},
                    "operations": [],
                }
            ),
            "must be arrays",
        ),
    ),
)
def test_operations_json_parser_rejects_invalid_envelopes(
    payload: bytes | str, message: str
) -> None:
    with pytest.raises(CAMValidationError, match=message):
        parse_operations_json(payload)


def test_operations_json_parser_accepts_canonical_document() -> None:
    document = valid_document()
    parsed = parse_operations_json(document.to_json())
    assert parsed["design_hash"] == document.design_hash


def test_cam_validator_rejects_tool_snapshot_tampering_and_non_exact_snapshots() -> None:
    document = valid_document()
    fingerprint_tamper = copy.copy(document)
    object.__setattr__(fingerprint_tamper, "tool_catalog_fingerprint", "0" * 64)
    fingerprint_result = validate_operations_document(fingerprint_tamper)
    assert any("fingerprint mismatch" in error for error in fingerprint_result.errors)

    unused_tool = replace(document.tools[0], tool_id="unused-tool")
    tools_with_unused = (*document.tools, unused_tool)
    extra_tool_document = replace(
        document,
        tools=tools_with_unused,
        tool_catalog_fingerprint=tool_catalog_fingerprint(tools_with_unused),
    )
    extra_tool_result = validate_operations_document(extra_tool_document)
    assert any(
        "snapshot does not exactly match operation tools" in error
        for error in extra_tool_result.errors
    )

    setup_with_unknown_tool = replace(
        document.setups[0], tool_ids=(*document.setups[0].tool_ids, "unknown-tool")
    )
    setup_document = replace(document, setups=(setup_with_unknown_tool,))
    setup_result = validate_operations_document(setup_document)
    assert any("setup references tool absent" in error for error in setup_result.errors)
    assert any("setup tool list does not exactly match" in error for error in setup_result.errors)


def test_machine_bound_validation_rejects_profile_setup_and_tool_drift() -> None:
    document = valid_document()
    machine = linuxcnc_reference_router_1325()

    wrong_profile = replace(document, machine_profile_id="unknown-router")
    assert any(
        "machine profile ID mismatch" in error
        for error in validate_operations_document(wrong_profile, machine=machine).errors
    )

    setup = replace(
        document.setups[0],
        safe_z_um=machine.safe_z_um + 1,
        wcs="G59",
        stock_width_um=machine.work_width_um + 1,
        origin=Point2D(1, 0),
    )
    setup_result = validate_operations_document(
        replace(document, setups=(setup,)),
        machine=machine,
    )
    for message in (
        "setup stock exceeds machine X travel",
        "setup safe Z does not match machine profile",
        "setup WCS is absent from machine profile",
        "setup origin must match the stock-frame origin",
    ):
        assert any(message in error for error in setup_result.errors)

    changed_tool = replace(document.tools[0], cutting_length_um=99_000)
    changed_tools = (changed_tool,)
    tool_result = validate_operations_document(
        replace(
            document,
            tools=changed_tools,
            tool_catalog_fingerprint=tool_catalog_fingerprint(changed_tools),
        ),
        machine=machine,
    )
    assert any(
        "selected tool differs from machine profile" in error for error in tool_result.errors
    )

    spoofed_machine = replace(machine, work_width_um=machine.work_width_um + 1)
    spoofed_result = validate_operations_document(document, machine=spoofed_machine)
    assert any(
        "differs from the trusted canonical profile" in error
        for error in spoofed_result.errors
    )


def test_cam_boundary_reports_document_setup_and_tool_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = valid_document()
    machine = linuxcnc_reference_router_1325()
    source_setup = document.setups[0]
    source_tool = document.tools[0]

    malformed_tool = copy.copy(source_tool)
    for field, value in (
        ("tool_id", ""),
        ("name", ""),
        ("version", ""),
        ("diameter_um", 0),
        ("cutting_length_um", 0),
        ("spindle_rpm", 0),
        ("feed_um_min", 0),
        ("plunge_um_min", 0),
        ("supported_operations", ()),
        ("measured_diameter_um", 0),
        ("runout_um", -1),
    ):
        object.__setattr__(malformed_tool, field, value)

    excessive_runout_tool = copy.copy(source_tool)
    object.__setattr__(excessive_runout_tool, "tool_id", "")
    object.__setattr__(
        excessive_runout_tool,
        "runout_um",
        source_tool.effective_diameter_um // 2,
    )
    object.__setattr__(
        excessive_runout_tool,
        "spindle_rpm",
        machine.max_spindle_rpm + 1,
    )

    malformed_setup = replace(
        source_setup,
        setup_id="",
        material_id="",
        material_version="",
        sheet_index=-1,
        side=Side.EDGE,
        wcs="G53",
        origin=Point2D(1, 0),
        stock_width_um=0,
        stock_height_um=machine.work_height_um + 1,
        safe_z_um=0,
        keep_out_zones=(Rect(0, 0, 0, 1_000),),
        tool_ids=("", ""),
    )
    conflicting_setup = replace(
        source_setup,
        setup_id=f"setup:{source_setup.stock_id}:000:EDGE",
        material_id="other-material",
        material_version="other-version",
        sheet_index=-1,
        side=Side.EDGE,
        stock_width_um=machine.work_width_um,
        stock_height_um=machine.work_height_um + 1,
        stock_thickness_um=machine.work_z_um,
        safe_z_um=machine.safe_z_um,
        tool_ids=("",),
    )

    mutated = copy.copy(document)
    object.__setattr__(mutated, "schema_version", "custombuild.operations.invalid")
    object.__setattr__(mutated, "machine_profile_version", "")
    object.__setattr__(mutated, "tool_catalog_version", "unversioned")
    object.__setattr__(mutated, "tools", (malformed_tool, excessive_runout_tool))
    object.__setattr__(mutated, "setups", (malformed_setup, conflicting_setup))

    def fail_canonicalization(_value: object) -> bytes:
        raise TypeError("synthetic canonicalization failure")

    monkeypatch.setattr(
        "custombuild_cam.validation.canonical_json_bytes",
        fail_canonicalization,
    )

    result = validate_operations_document(mutated, machine=machine)
    errors = "\n".join(result.errors)

    assert result.valid is False
    for message in (
        "unsupported operations schema_version",
        "versioned machine profile identity is required",
        "duplicate tool_id in selected-tool snapshot",
        "selected-tool snapshot cannot be canonicalized",
        "selected-tool snapshot has no catalogue version",
        "setup and stock identity are required",
        "setup material identity is required",
        "negative setup sheet index",
        "setup identity is not traceable to stock, sheet and side",
        "non-positive setup stock dimensions",
        "non-positive setup safe Z",
        "unsupported setup WCS",
        "edge setup is unsupported by the 3-axis CAM boundary",
        "duplicate tool ID in setup",
        "setup contains a non-canonical tool ID",
        "invalid keep-out zone in setup",
        "setup stock exceeds machine Y travel",
        "setup stock and safe Z exceed machine Z travel",
        "setup side is unsupported by machine profile",
        "setups disagree on stock/material binding",
        "operations document machine profile version mismatch",
        "selected-tool snapshot catalogue version differs from machine profile",
        "selected tool exceeds machine spindle limit",
        "selected tool has a blank or non-canonical ID",
        "selected tool has a blank name",
        "selected tool has a blank or non-canonical version",
        "selected tool has invalid dimensions or cutting parameters",
        "selected tool operations are empty or duplicated",
        "selected tool has an invalid measured diameter",
        "selected tool has invalid runout",
        "selected tool runout reaches the cutter radius",
    ):
        assert message in errors


def test_cam_boundary_reports_independent_operation_safety_tampering() -> None:
    document = valid_document()
    machine = linuxcnc_reference_router_1325()
    setup = document.setups[0]
    source = document.operations[0]
    tool = document.tools[0]
    operation_prefix = f"op:{source.instance_id}:{source.feature_id}"

    too_shallow = replace(
        source,
        operation_id=f"{operation_prefix}:001",
        through=True,
        depth_um=setup.stock_thickness_um - 1,
    )
    excessive_overtravel = replace(
        source,
        operation_id=f"{operation_prefix}:002",
        through=True,
        depth_um=setup.stock_thickness_um + 501,
    )
    unsafe_metadata = replace(
        source,
        operation_id=f"{operation_prefix}:003",
        stepdown_um=tool.effective_diameter_um + 1,
        stepover_ppm=0,
        tolerance_um=-1,
        fit_clearance_um=-1,
        corner_strategy="unversioned-corner-strategy",
        corner_relief_radius_um=0,
        open_end_reliefs=("bad-boundary", "bad-boundary"),
    )
    excessive_z = replace(
        source,
        operation_id=f"{operation_prefix}:004",
        depth_um=machine.work_z_um - setup.safe_z_um + 1,
    )
    mismatched_drill = replace(
        source,
        operation_id=f"{operation_prefix}:005",
        diameter_um=tool.effective_diameter_um + machine.accuracy_um + 1,
    )
    missing_area_envelope = replace(
        source,
        operation_id=f"{operation_prefix}:006",
        kind=OperationKind.POCKET,
        diameter_um=None,
        width_um=20_000,
        length_um=20_000,
        cutter_envelope_x_um=None,
        cutter_envelope_y_um=None,
        cutter_envelope_width_um=None,
        cutter_envelope_length_um=None,
    )
    mutated = replace(
        document,
        operations=(
            too_shallow,
            excessive_overtravel,
            unsafe_metadata,
            excessive_z,
            mismatched_drill,
            missing_area_envelope,
        ),
    )

    result = validate_operations_document(mutated, machine=machine)
    errors = "\n".join(result.errors)

    assert result.valid is False
    for message in (
        "through operation does not reach stock depth",
        "through operation exceeds spoilboard allowance",
        "operation stepover is outside (0, 1]",
        "operation tolerance or clearance is negative",
        "operation corner strategy is unsupported",
        "operation corner relief radius is invalid",
        "operation open-end relief declaration is invalid",
        "operation stepdown exceeds cutter diameter",
        "operation exceeds machine Z travel",
        "drilling diameter differs from selected tool",
        "area operation missing cutter envelope",
    ):
        assert message in errors


def test_cam_validation_rejects_depth_stepdown_cutter_and_keepout_tampering() -> None:
    document = valid_document()
    operation = document.operations[0]

    unsafe_depth = replace(
        operation,
        depth_um=99_000,
        stepdown_um=-1,
    )
    depth_result = validate_operations_document(replace(document, operations=(unsafe_depth,)))
    assert any("non-through operation reaches stock depth" in e for e in depth_result.errors)
    assert any("requires a positive stepdown" in e for e in depth_result.errors)
    assert any("selected-tool cutting length" in e for e in depth_result.errors)

    keepout_setup = replace(
        document.setups[0],
        keep_out_zones=(
            Rect(operation.x_um - 5_000, operation.y_um - 5_000, 10_000, 10_000),
        ),
    )
    keepout_result = validate_operations_document(
        replace(document, setups=(keepout_setup,))
    )
    assert any("intersects setup keep-out zone" in e for e in keepout_result.errors)

    area_tool = replace(
        document.tools[0],
        supported_operations=(*document.tools[0].supported_operations, OperationKind.POCKET),
    )
    area_operation = replace(
        operation,
        kind=OperationKind.POCKET,
        diameter_um=None,
        width_um=20_000,
        length_um=20_000,
        cutter_envelope_x_um=105_000,
        cutter_envelope_y_um=105_000,
        cutter_envelope_width_um=5_000,
        cutter_envelope_length_um=5_000,
    )
    area_result = validate_operations_document(
        replace(
            document,
            operations=(area_operation,),
            tools=(area_tool,),
            tool_catalog_fingerprint=tool_catalog_fingerprint((area_tool,)),
        )
    )
    assert any("cutter envelope does not contain operation" in e for e in area_result.errors)


def test_cam_boundary_rejects_empty_documents_and_untraceable_identities() -> None:
    document = valid_document()
    empty = replace(
        document,
        setups=(),
        operations=(),
        tools=(),
        tool_catalog_fingerprint=tool_catalog_fingerprint(()),
    )

    empty_result = validate_operations_document(empty)

    assert any("at least one setup" in error for error in empty_result.errors)
    assert any("at least one operation" in error for error in empty_result.errors)

    operation = document.operations[0]
    for field in ("operation_id", "part_id", "instance_id", "feature_id"):
        invalid_operation = copy.copy(operation)
        object.__setattr__(invalid_operation, field, "")
        result = validate_operations_document(
            replace(document, operations=(invalid_operation,))
        )
        assert any(f"non-canonical {field}" in error for error in result.errors)

    untraceable = replace(operation, operation_id="op:another-instance:another-feature")
    trace_result = validate_operations_document(replace(document, operations=(untraceable,)))
    assert any("not traceable to instance and feature" in error for error in trace_result.errors)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"reference_surface": ""}, "unsupported reference surface"),
        ({"fixture": ""}, "unsupported or blank fixture"),
        ({"probe_method": ""}, "probe method is unsupported or blank"),
        ({"orientation": "G92 X0; M3"}, "orientation does not match"),
        ({"operator_steps": ("",)}, "operator steps are blank or unsafe"),
    ),
)
def test_cam_boundary_rejects_unsafe_setup_claims(
    changes: dict[str, object],
    message: str,
) -> None:
    document = valid_document()
    setup = copy.copy(document.setups[0])
    for field, value in changes.items():
        object.__setattr__(setup, field, value)

    result = validate_operations_document(replace(document, setups=(setup,)))

    assert any(message in error for error in result.errors)


def test_cam_boundary_requires_canonical_compensation_and_release_holding() -> None:
    document = valid_document()
    machine = linuxcnc_reference_router_1325()
    contour_tool = next(
        tool for tool in machine.tools if OperationKind.CONTOUR in tool.supported_operations
    )
    setup = replace(document.setups[0], tool_ids=(contour_tool.tool_id,))
    source = document.operations[0]
    contour = replace(
        source,
        tool_id=contour_tool.tool_id,
        kind=OperationKind.CONTOUR,
        depth_um=setup.stock_thickness_um,
        diameter_um=None,
        width_um=20_000,
        length_um=30_000,
        cutter_envelope_x_um=source.x_um,
        cutter_envelope_y_um=source.y_um,
        cutter_envelope_width_um=20_000,
        cutter_envelope_length_um=30_000,
        stepdown_um=3_000,
        through=True,
        compensation="OUTSIDE",
        holding_strategy="TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
    )
    contour_document = replace(
        document,
        setups=(setup,),
        operations=(contour,),
        tools=(contour_tool,),
        tool_catalog_fingerprint=tool_catalog_fingerprint((contour_tool,)),
    )
    assert validate_operations_document(contour_document).valid

    injected = replace(contour, compensation="G41;M3")
    injected_result = validate_operations_document(
        replace(contour_document, operations=(injected,))
    )
    assert any("compensation is unsupported" in error for error in injected_result.errors)

    unheld = replace(contour, holding_strategy=None)
    unheld_result = validate_operations_document(
        replace(contour_document, operations=(unheld,))
    )
    assert any("release holding strategy" in error for error in unheld_result.errors)

    drill_with_contour_claims = replace(
        source,
        compensation="OUTSIDE",
        holding_strategy="TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
    )
    non_contour_result = validate_operations_document(
        replace(document, operations=(drill_with_contour_claims,))
    )
    assert any(
        "non-contour operation declares compensation" in error
        for error in non_contour_result.errors
    )
    assert any("holding strategy is unsupported" in error for error in non_contour_result.errors)


@pytest.mark.parametrize(
    "payload",
    (
        "%\nG21\nM5\nG0 Z15\nM30\n%\n",
        "%\nG21\nG90\nG0 Z15\nM5\nM30\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15\nG0 X1\nM30\nG0 Z15\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15 Z16\nM30\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15\nG0 X1 X2\nM30\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15\nX1\nM30\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15\nO100\nM30\n%\n",
        "%\nG21\nG90\nM5\nG0 Z15\n%\n",
    ),
)
def test_gcode_safety_validator_rejects_modal_and_structural_risks(payload: str) -> None:
    with pytest.raises(GCodeSafetyError):
        validate_validation_program(
            payload,
            required_safe_z_mm=Decimal("15"),
            maximum_z_mm=Decimal("15"),
            x_bounds_mm=(Decimal("0"), Decimal("1000")),
            y_bounds_mm=(Decimal("0"), Decimal("600")),
            required_wcs="G54",
        )


@pytest.mark.parametrize(
    "payload",
    (
        "G0 X#1",
        "(nested (comment))",
        "unmatched)",
        "(unclosed",
        b"\xff",
    ),
)
def test_gcode_parser_rejects_unsupported_syntax(payload: bytes | str) -> None:
    with pytest.raises(GCodeParseError):
        parse_gcode(payload)


def test_validation_only_models_reject_production_claims() -> None:
    with pytest.raises(ValueError, match="validation-only"):
        ValidationBackplot("PRODUCTION", (), ())
    with pytest.raises(ValueError, match="validation-only"):
        MachineProgram("x.ngc", "s", "LinuxCNC", "1", "PRODUCTION", b"", False)
    with pytest.raises(ValueError, match="cannot approve"):
        MachineProgram("x.ngc", "s", "LinuxCNC", "1", "VALIDATION_DRY_RUN", b"", True)
