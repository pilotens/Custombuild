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
    PartSpec,
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


def valid_document():
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
    )
    outside = replace(
        missing_extent,
        operation_id="area-outside",
        x_um=990_000,
        y_um=590_000,
        width_um=20_000,
        length_um=20_000,
    )
    invalid = replace(document, operations=(missing_extent, outside))

    result = validate_operations_document(invalid)

    assert any("missing extent" in error for error in result.errors)
    assert any("envelope outside stock" in error for error in result.errors)

    valid_area = replace(
        missing_extent,
        operation_id="area-valid",
        x_um=100_000,
        y_um=100_000,
        width_um=20_000,
        length_um=30_000,
    )
    area_document = replace(document, operations=(valid_area,))
    envelope = theoretical_removal_envelopes(area_document)[0]
    assert (envelope.x_max_um, envelope.y_max_um) == (120_000, 130_000)
    assert len(build_validation_backplot(area_document).moves) == 6


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"\xff", "invalid UTF-8"),
        ("[]", "JSON object"),
        ('{"schema_version":"wrong"}', "schema_version"),
        ('{"schema_version":"custombuild.operations.v1","mode":"PRODUCTION"}', "validation-only"),
        (
            '{"schema_version":"custombuild.operations.v1","mode":"VALIDATION"}',
            "missing design_hash",
        ),
        (
            json.dumps(
                {
                    "schema_version": "custombuild.operations.v1",
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
        validate_validation_program(payload, required_safe_z_mm=Decimal("15"))


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
